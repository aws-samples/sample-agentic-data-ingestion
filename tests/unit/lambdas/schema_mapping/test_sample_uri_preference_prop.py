# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: the URI preference is a preference, not a replacement.

p53 task 4.1, **Property 3**. Consumers 2 and 4, both in this one Lambda:

* **consumer 2** — the mapping prompt's sampled values,
  ``schema_mapping/lambda_function.py:2104``, feeding ``read_source_samples`` at
  ``:2107``;
* **consumer 4** — the complex_mapping_agent payload's ``sample_data_uri``,
  ``schema_mapping/lambda_function.py:2284``. The consumer the commissioning
  brief did not name.

**Validates: Requirements 2.4, 2.5, 2.6**

THE ONE THING THIS FILE CAN TEST THAT NO EXAMPLE TEST CAN
---------------------------------------------------------
The two sites resolve the same preference **by different means**, and that
asymmetry is stated in the design (§"Testing Strategy": consumer 4 "needs its own
case, because it is a ``.get()`` with a ``""`` default rather than an
``item_str``"):

* ``:2104`` — ``item_str(Item, "sample_s3_uri") or item_str(Item, "json_s3_uri")``
* ``:2284`` — ``schema_item.get("sample_s3_uri") or schema_item.get("json_s3_uri", "")``

Task 3.2's four example tests for these sites live in **two different files**
(``test_mapping_prompt_samples.py``, ``test_schema_mapping_complex_agent.py``)
with two different fixtures and two different URI constants, so nothing in the
tree currently asserts that the two expressions **agree**. Every property below
drives ONE handler run — a low-confidence mapping with the agent endpoint
configured, which reaches both sites — and asserts both observations against the
same expectation. Divergence is the defect that would otherwise ship silently,
because each site's own example tests would keep passing.

Two consequences worth stating rather than discovering later:

* ``test_schema_mapping_complex_agent.py:47-53`` stubs ``dynamo_typing`` with
  ``item_str=lambda item, key: item.get(key, "")``, which is **not** the real
  ``item_str`` — the real one also returns ``""`` for a present-but-non-string
  value. Under that stub the two expressions are identical by construction and
  the agreement claim is unfalsifiable. This file therefore loads the **real**
  ``shared/dynamo_typing`` and stubs nothing that the preference reads.
* The agreement holds over **string** values, which is the space R2.4/R2.5
  govern and the only space the writer produces
  (``_probe_sample_artifact_uri`` returns ``str``, ``schema_extract:1517``). It
  does **not** hold for a non-string ``sample_s3_uri``: ``item_str`` reads it as
  absent, the raw ``.get`` reads it as present. That divergence is recorded at
  ``test_the_two_sites_agree_over_the_governed_string_space`` as an out-of-space
  observation rather than asserted, because asserting either answer would be
  inventing a requirement.

MODULE LOADING — a private copy, never published to sys.modules
---------------------------------------------------------------
``lambda_function.py`` is a name 23 Lambda directories share.
``LGR-c58f276-4`` (**template / ratified**) records the defect class that causes
— what a module-scope ``sys.modules`` stub in one test file does to a property
test in another — and ratifies the remedy used here: load the file with
``importlib.util.spec_from_file_location`` under a private alias that is never
published, then assert at import time that what was bound is neither a mock nor
vacuous. ``tests/unit/templates/test_source_sample_bounds_prop.py`` (p53 task
2.2) loads this same module the same way. The helpers are deliberately local
copies rather than a shared import, matching this layer's convention
(``test_schema_compliance_gate_prop.py:162``).

WHAT IS NOT TOUCHED
-------------------
``read_source_samples`` keeps its signature, its caps and its
``{}``-never-raise contract (R2.1, R2.2, R2.3) — task 3.2 changed the *caller*,
and so does this file's subject. The agent's own reader,
``agentcore/complex_mapping_agent/main.py::read_sample_data``, is not exercised
here at all and keeps its WARNING log level (p53 C4 records why that warning is
expected on run 1 of a JDBC pipeline and why moving it is a separate judgement).

Nothing in this file changes production code and nothing in it amends a
pre-existing assertion.
"""

from __future__ import annotations

import importlib.util
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
_SCHEMA_MAPPING = os.path.join(_REPO, "lambda", "agentic_data_ingestion_schema_mapping")

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
    "_p53_4_1_schema_mapping_lambda_function",
    os.path.join(_SCHEMA_MAPPING, "lambda_function.py"),
    {"env_utils": _ENV_UTILS_STUB},
    [_SHARED, _SCHEMA_MAPPING],
)

# Import-time non-vacuity guards, per LGR-c58f276-4. `hasattr` alone is not a
# guard — a MagicMock has every attribute — so the loaded module is also asked
# questions with known answers. The `item_str` probes are the load-bearing ones
# here: bound to the simplified stub the sibling example file installs, this
# file's central claim (the two sites AGREE) becomes unfalsifiable.
assert not isinstance(_MOD, MagicMock), "schema_mapping resolved to a mock, not the real module"
for _attr in ("lambda_handler", "read_source_samples", "item_str", "complex_mapping_threshold"):
    assert hasattr(_MOD, _attr), f"the real schema_mapping has no {_attr}; the private load was defeated"
assert _MOD.item_str({"k": "v"}, "k") == "v", "the loaded item_str is not the real one"
assert _MOD.item_str({"k": 7}, "k") == "", (
    "the loaded item_str returns a non-string through; it is the simplified stub from "
    "test_schema_mapping_complex_agent.py:47-53, not shared/dynamo_typing.py, and the "
    "site-agreement property would be vacuous against it"
)
assert _MOD.read_source_samples("jdbc://pipeline/probe") == {}, (
    "the loaded read_source_samples does not decline a jdbc:// URI; it is not the real reader"
)


# ---------------------------------------------------------------------------
# The handler harness
# ---------------------------------------------------------------------------

RUN_ID = "run-p53-4-1"
PIPELINE_ID = "pipe-p53-4-1"

_AGENT_ENDPOINT = (
    "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/complex-mapping-p53/invocations?qualifier=DEFAULT"
)

#: Below the 0.7 threshold at ``:2255``, which is what makes consumer 4's block
#: reachable in the same run as consumer 2's read. Without this the agent branch
#: never fires and half the file would be silently untested.
_LOW_CONFIDENCE_MAPPING = {
    "mappings": [
        {"source_field": "cust_id", "target_field": "customer_id", "confidence": 0.55, "transformation": "direct"},
        {"source_field": "fname", "target_field": "first_name", "confidence": 0.60, "transformation": "direct"},
    ],
    "unmapped_source_fields": [],
    "unmapped_target_fields": [],
    "mapping_summary": "Low confidence mapping",
}

_SOURCE_SCHEMA = {"properties": {"cust_id": {"type": "string"}, "fname": {"type": "string"}}}
_TARGET_SCHEMA = {"properties": {"customer_id": {"type": "string"}, "first_name": {"type": "string"}}}

_AGENT_RESPONSE = {"evidence": {}, "questions": [], "mappings": []}

_PATCH_TARGETS = (
    "get_source_schema",
    "get_pipeline_metadata",
    "get_target_schema",
    "normalize_schema_format",
    "get_ai_model_for_pipeline",
    "generate_mapping_id",
    "store_mapping_separately",
    "determine_approval_requirement",
    "store_mapping_result",
    "publish_pipeline_state_change",
    "publish_approval_status_change",
    "write_agent_audit_entry",
    "AgentTracePlugin",
)


class _nested:
    """Enter a mapping of named context managers, exit them in reverse.

    ``__enter__`` returns the dict of values the managers *yielded* (the mocks),
    keyed by the same names. Same shape as
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
        "run_id": RUN_ID,
        "step_name": "schema_extract",
        "original_pipeline_id": PIPELINE_ID,
        "json_s3_uri": json_uri,
    }
    if sample_shape == "empty":
        item["sample_s3_uri"] = ""
    elif sample_shape == "present":
        item["sample_s3_uri"] = sample_uri
    return item


def _resolved_uris(item: dict) -> tuple[str, str]:
    """One handler run against *item*; ``(consumer_2_uri, consumer_4_uri)``.

    Consumer 2 is observed at the ``read_source_samples`` seam (``:2107``),
    consumer 4 at the ``invoke_agentcore_runtime`` payload (``:2296``). Both come
    off the same run and the same Item, which is the whole point of driving them
    together.
    """
    invoke = MagicMock(return_value=_AGENT_RESPONSE)
    agentcore_stub = MagicMock(invoke_agentcore_runtime=invoke)

    managers = {name: patch.object(_MOD, name) for name in _PATCH_TARGETS}
    managers["table"] = patch.object(_MOD, "table", MagicMock(get_item=MagicMock(return_value={"Item": item})))
    managers["memory_client"] = patch.object(_MOD, "memory_client", None)
    managers["read_source_samples"] = patch.object(_MOD, "read_source_samples", return_value={})
    managers["generate_mapping_with_bedrock"] = patch.object(
        _MOD, "generate_mapping_with_bedrock", return_value=_LOW_CONFIDENCE_MAPPING
    )

    with (
        _ScopedStubs({"agentcore_proxy": agentcore_stub}),
        patch.dict(
            os.environ,
            {"COMPLEX_MAPPING_AGENT_ENDPOINT": _AGENT_ENDPOINT, "COMPLEX_MAPPING_THRESHOLD": "0.7"},
        ),
        _nested(managers) as mocks,
    ):
        mocks["get_source_schema"].return_value = _SOURCE_SCHEMA
        mocks["get_pipeline_metadata"].return_value = {"pipeline_id": PIPELINE_ID, "target_schema_name": "t"}
        mocks["get_target_schema"].return_value = (_TARGET_SCHEMA, "tgt-hash")
        mocks["normalize_schema_format"].side_effect = lambda schema: schema
        mocks["get_ai_model_for_pipeline"].return_value = "us.test-model"
        mocks["generate_mapping_id"].return_value = "map-p53"
        mocks["determine_approval_requirement"].return_value = False
        mocks["AgentTracePlugin"].return_value = MagicMock()

        result = _MOD.lambda_handler(
            {
                "run_id": RUN_ID,
                "pipeline_id": PIPELINE_ID,
                "source_schema_hash": "src-hash-p53",
                "additional_context": "",
            },
            None,
        )
        read = mocks["read_source_samples"]

    assert result["statusCode"] == 200, f"the handler did not complete: {result}"
    assert read.call_count >= 1, "consumer 2's reader was never reached; the property would be vacuous"
    assert invoke.called, (
        "consumer 4's agent block was never reached — the mapping's confidence or the "
        "endpoint env var stopped being what this harness assumes, and half of this "
        "file would be silently vacuous"
    )
    return read.call_args.args[0], invoke.call_args.kwargs["payload"]["sample_data_uri"]


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------
#
# Both URIs are generated rather than fixed. A constant fallback lets a
# constant-returning implementation pass, and a constant sample lets a site that
# reads the wrong key pass whenever the two happen to coincide. The alphabets are
# narrow on purpose — the subject is WHICH attribute is chosen and whether it
# survives byte-for-byte, not URI parsing, which `read_source_samples` owns and
# R2.1-R2.3 forbid changing.

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
    ``samples/{pipeline_id}/source-sample.json`` (``schema_extract:1513``,
    ``:1542``).

    Generating the *shape* rather than arbitrary strings is what makes a pass
    mean something about real artifact URIs, and it guarantees distinctness from
    every fallback below.
    """
    return f"s3://adi-processed-files-{draw(_ACCOUNT)}-{draw(_REGION)}/samples/{draw(_SLUG)}/source-sample.json"


@st.composite
def _fallback_uris(draw) -> str:
    """``json_s3_uri``'s own value, across all three forms it really takes.

    * an ``s3://`` upload object — an S3_UPLOAD pipeline, where the preference
      changes nothing and must therefore change nothing;
    * ``jdbc://{engine}/{schema}/{table}`` — the ``original_s3_uri``
      ``file_validation`` writes for a JDBC source;
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

    If the two URIs could be equal, a site that ignored ``sample_s3_uri``
    entirely would satisfy the ``present`` case by coincidence. Asserted rather
    than assumed, so a later widening of either generator cannot quietly hollow
    the property out.
    """
    assert sample_uri != json_uri, "generated URIs collided; the present case would be vacuous"
    assert sample_uri.startswith("s3://"), "a Sample_Artifact URI is always an s3:// object URI"


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------
#
# Feature: p53-jdbc-source-sampling, Property 3: The URI preference is a
# preference, not a replacement.


@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(row=_schema_rows())
def test_the_uri_preference_is_a_preference_not_a_replacement(row):
    """Property 3 at consumers 2 and 4, over all three row shapes.

    Half 1 — *for any* schema row, the URI each site resolves is
    ``sample_s3_uri`` when that attribute is present and non-empty, and
    ``json_s3_uri`` otherwise (R2.4, R2.5).

    Half 2 — do-NOT 11 / design C3: the preference *reads* ``json_s3_uri``, it
    never rewrites, clears or consumes it. Asserted in the ``present`` case as
    much as the absent one, because ``present`` is the only shape in which a
    repoint would be tempting, and it is the shape
    ``dashboard_api/handlers/data_preview.py:55-73``,
    ``dashboard_api/handlers/files.py:83-89`` and ``schema_extract:1861-1870``
    would break on (R2.6).

    Both halves come off one handler run — see the module docstring.

    **Validates: Requirements 2.4, 2.5, 2.6**
    """
    shape, sample_uri, json_uri, expected = row
    _assert_the_pair_is_a_real_choice(sample_uri, json_uri)

    item = _schema_extract_item(shape, sample_uri, json_uri)
    consumer_2, consumer_4 = _resolved_uris(item)

    assert consumer_2 == expected
    assert consumer_4 == expected

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


@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(row=_schema_rows())
def test_the_two_sites_agree_over_the_governed_string_space(row):
    """``item_str``-based ``:2104`` and raw-``.get``-based ``:2284`` never diverge.

    The two expressions are written differently — the design says so explicitly —
    so "they agree" is a claim, not a tautology. Nothing in the tree asserted it
    before this file, because task 3.2's four example tests for these sites live
    in two different files with two different URI constants.

    **The boundary of the claim, stated rather than left to be discovered.** It
    holds over **string** values, which is the space R2.4/R2.5 govern and the
    only space the writer produces: ``_probe_sample_artifact_uri`` returns
    ``str`` (``schema_extract:1517``) and ``store_schema_in_dynamodb`` writes
    exactly what it was handed (``:1367-1368``). For a present-but-non-string
    ``sample_s3_uri`` the two sites DO diverge — ``item_str`` reads it as absent
    and falls back, the raw ``.get`` reads it as present and passes it through.
    That is not asserted either way here: no requirement says what a non-string
    should mean, and inventing one in a test is how a fence gets built by
    accident. It is reported as a finding of this task instead.

    **Validates: Requirements 2.4, 2.5**
    """
    shape, sample_uri, json_uri, _expected = row
    _assert_the_pair_is_a_real_choice(sample_uri, json_uri)

    consumer_2, consumer_4 = _resolved_uris(_schema_extract_item(shape, sample_uri, json_uri))

    assert consumer_2 == consumer_4


@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(sample_uri=_sample_artifact_uris(), json_uri=_fallback_uris())
def test_absent_and_empty_resolve_identically_at_both_sites(sample_uri, json_uri):
    """R2.5 spelled out: ``""`` and "no key at all" are one case, not two.

    The DynamoDB row omits the key (``schema_extract:1367``) while the return
    envelope carries ``""`` (``:1836``), so both shapes reach a consumer
    depending on which writer produced the row. A preference keyed on key
    *presence* rather than on truthiness would pass every example test task 3.2
    wrote and still diverge here.

    **Validates: Requirements 2.5**
    """
    _assert_the_pair_is_a_real_choice(sample_uri, json_uri)

    absent = _resolved_uris(_schema_extract_item("absent", sample_uri, json_uri))
    empty = _resolved_uris(_schema_extract_item("empty", sample_uri, json_uri))

    assert absent == empty == (json_uri, json_uri)
