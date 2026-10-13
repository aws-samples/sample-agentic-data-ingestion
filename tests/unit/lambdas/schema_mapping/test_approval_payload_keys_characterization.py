# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 task 1.2 — characterization of the schema-mapping approval payload's
documented keys, pinned BEFORE W4 adds the Rename_Manifest key to it.

**Function under test, named by file and line because two functions in this
repository carry this name:** `create_approval_payload` in
`lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:1677`. The other
is `create_approval_payload` in
`lambda/agentic_data_ingestion_create_code/lambda_function.py:521`, and it is NOT
the one W4 changes. **Both verified present at those lines by reading this tree**,
so for once the spec's two figures need no correction.

Why the disambiguation is load-bearing rather than pedantic: risk R5.8 is that a
grep-driven edit changes the wrong one. `codegraph query create_approval_payload`
returns *both* definitions, and `codegraph callers create_approval_payload`
returns two callers without saying which belongs to which — so the tool that
would normally settle it does not. The signatures are the cheap discriminator and
this module asserts on it (`test_this_is_the_schema_mapping_function_and_not_create_codes`):

* schema_mapping's takes five positional parameters, all schema/mapping shaped —
  `(source_schema, target_schema, mapping_result, source_schema_hash,
  target_schema_hash)` (`:1678`).
* create_code's takes eight, all code shaped — `code_version_id`,
  `generated_code`, `code_quality_metrics`, `agent_reasoning`, `template_used`,
  `code_s3_uri`, `dqdl_rules=""`, `requires_review=True` (`:522-529`).

Verified line numbers for everything asserted below, since line references drift
in this tree:

* `:1677` `def create_approval_payload`, docstring `:1680-1695`, `try:` at `:1696`.
* `:1732-1778` the payload dict literal — **thirteen** top-level keys.
* `:1780` `return approval_payload`; `:1782-1788` the degraded `except` arm.
* `:1882` `def store_mapping_result`, the sole caller, calling at `:1902` — the
  spec's `store_mapping_result:1902` is the *call* line, and the `def` is at
  `:1882`. Both are correct readings of different things; recorded so neither
  looks like an error.
* `:1927` is where `store_mapping_result` puts the whole payload:
  `output_data["approval"] = convert_floats_to_decimal(approval_payload)`. That
  one line is why the key set matters beyond this Lambda.

**Expectations derive from the payload's documented keys, not from a captured
dict.** A characterization test built by dumping today's output ratifies whatever
is wrong with it. Each assertion below cites where its expectation is documented:

* The **seven** keys the approval read path documents as coming from this
  producer — `source_schema`, `target_schema`, `proposed_mappings`,
  `confidence_scores`, `unmapped_fields`, `mapping_summary`, `generated_by` — are
  enumerated verbatim in `tests/fixtures/conftest.py:636-643` and again in
  `tests/unit/lambdas/approval_handler/test_schema_mapping_payload_read.py:31-35`.
* `evidence` and `questions` are documented in those same two places as
  **NOT** this producer's, their writer being the complex-mapping agent's
  top-level `update_item`.
* `unmapped_fields`' four keys come from this function's own docstring
  (`:1682-1694`, the p37 R3.2 block) and from
  `tests/contracts/api_responses/test_unsourced_keys_contract.py:6-9`, which
  states "four keys, two of them" p37's.
* The statistic names' meanings are read off the names plus the derivations
  written as comments beside the fixture at `tests/fixtures/conftest.py:857-867`
  (`# round(4 / 5 * 100, 1)` for `mapping_coverage_percent`).

**Coverage correction, and it is the same shape task 1.1 found.** The spec says
"CodeGraph reports **neither** as covered" and design's coverage table says "Not
verified as covered". For create_code's function that is not re-checked here. For
**schema_mapping's it is a false flag**: `test_schema_mapping_approval.py:562`,
`:576` and `:610` all call this exact function, and
`tests/contracts/api_responses/test_unsourced_keys_contract.py` names it as its
producer and AST-parses its `unmapped_fields` literal. So coverage exists — it is
scoped to p37's two new keys and to the fallback-provenance path
(`test_schema_mapping_fallback_provenance.py:250`), and nothing asserted the
**key set** the downstream read path depends on. That is the gap this file fills.

One stale artifact found while establishing that, recorded but not edited, since
this task changes one new file: `test_schema_mapping_approval.py:541-544` states
`create_approval_payload` "is a CONFIRMED zero-coverage finding, not a CodeGraph
false negative" — true when written, no longer true, because that very file is
now its coverage.

**Deliberately NOT asserted: the degraded `except` arm (`:1782-1788`).** The
blanket `except Exception` returns `{approval_type, error, generated_by}` and is
genuinely reachable — `field_mapping_details` indexes `m["source_field"]`
(`:1716`) and `confidence_scores` indexes `m["target_field"]` (`:1707`), so a
mapping entry missing either key degrades the whole payload. Every test here
instead asserts `logger.error` was never called (its call at `:1783` is the only
`logger.error` in the function), so a swallow-manufactured return **fails** these
tests rather than being recorded as the documented shape. Same posture as the
sibling `test_approval_ratio_clauses_characterization.py` and as
`test_schema_mapping_approval.py:548-559`, which declines to pin the same arm and
cites the record for it.

**Cite `LGR-b2f79ef-13`, not `LGR-78ce0b7-13`.** That record (template /
ratified) narrowed a blanket `except Exception` in this same module which
re-labelled an already-classified exception out of `get_target_schema`
(`:303-305`) — a reasoning precedent on this file, not a direct fence on these
lines. The `78ce0b7` hash is a dead twin that resolves to nothing;
`LGR-7561acb-6` (template / ratified) documents the substitution. The sibling
comment at `test_schema_mapping_approval.py:557` still carries the dead id and is
left alone.

**The one record that governs content here is `LGR-9e76249-7`** (template /
ratified; BROAD, 80 files, so read as background). It is the p37 C10/C9 ordering
decision, and it is what put `target_unsourced` and `target_required` in this
payload: "`unsourced_target_field_names` reads **both** lists … and the approval
decision, the approval payload and the normaliser all call it, so they cannot
drift about what 'unsourced' means." Test 4 below is that no-drift property read
off the payload rather than off the reader.

**Ledger state for this function specifically: unfenced.** `why` on
`schema_mapping/lambda_function.py:1677` returns one commit (`8f3f9e1b30`, p12a
bug cleanup) with `records: []` — no decision content. `why` on the file returns
`LGR-5c539a6-*`, `LGR-1e135d2-*`, `LGR-b397ee2-*`, `LGR-cad8032-*` and
`LGR-9e76249-7`; only the last says anything about this payload. A thin result is
a gap in the ledger, not permission to move things.

**Requirement citation.** Task 1.2's footer cites `_Requirements: 12.1, 12.2_`,
but R12.1/R12.2 (`requirements.md:453-454`) are the Identifier_Allowlist criteria
— an identifier failing the allowlist raises naming it, and `validate_identifier`
returns its input unmodified — which design §7 assigns to **task 6.7**
("R12.1, R12.2 characterization of the loud-failure path"). Neither is about this
payload, and this file asserts nothing about either. What this task actually
serves is design §7's own row for 1.2 and risk R5.8: characterize
`schema_mapping.create_approval_payload` before W4 (task 6.2) adds the
Rename_Manifest key to it. Recorded rather than silently followed either way.

**Note for task 6.2, recorded not asserted:** the key it adds lands on the happy
path only. The degraded arm above returns three keys and no `unmapped_fields` at
all, so any consumer of the new key must tolerate its absence — the same
tolerance `test_unsourced_keys_contract.py:444-448` already documents for p37's
keys. (That file cites `lambda_function.py:1433-1437` for the degraded return;
verified at `:1784-1788` in this tree. Its figure is stale, not wrong about the
three keys.)

Production code is unchanged by this file.

**Validates: design §7 task 1.2, risk R5.8**
"""

import inspect
import os
import sys
from unittest.mock import MagicMock, patch

# Region is pinned by tests/unit/conftest.py (LGR-3e45e65-1); these are no-ops
# kept for parity with the sibling files in this directory.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

_schema_mapping_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_mapping")
)
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Lambda-layer-only dependencies (the pattern in the sibling files here).
sys.modules.setdefault(
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock(PipelineMetrics=None))
sys.modules.setdefault("mapping_memory", MagicMock(MappingMemoryClient=None))

# sys.modules.pop + sys.path.insert: `lambda_function` is a name 23 Lambda dirs
# share, so it cross-contaminates between test files. Every patch below is
# `patch.object(_mod, …)` rather than `@patch("lambda_function.…")`, so nothing
# here depends on sys.modules still pointing at this module at call time.
sys.modules.pop("lambda_function", None)
sys.path.insert(0, _schema_mapping_dir)
try:
    import lambda_function as _mod
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

#: The seven keys the approval read path documents as coming from this producer —
#: `tests/fixtures/conftest.py:636-643` and
#: `approval_handler/test_schema_mapping_payload_read.py:31-35`. Not the whole key
#: set: the payload carries thirteen top-level keys (`:1732-1778`). These seven
#: are the ones a consumer outside this Lambda reads.
_DOCUMENTED_CONSUMED_KEYS = frozenset(
    {
        "source_schema",
        "target_schema",
        "proposed_mappings",
        "confidence_scores",
        "unmapped_fields",
        "mapping_summary",
        "generated_by",
    }
)

#: Documented in the same two places as NOT this producer's keys — their writer is
#: the complex-mapping agent's top-level `update_item` (`:2277-2291` per those
#: files). Asserting their absence is what keeps the producer boundary legible
#: when W4 adds a key here.
_KEYS_THIS_PRODUCER_MUST_NOT_OWN = ("evidence", "questions")

#: This function's own docstring (`:1682-1694`) plus
#: `contracts/api_responses/test_unsourced_keys_contract.py:6-9`: four keys, two
#: of them p37's.
_DOCUMENTED_UNMAPPED_FIELDS_KEYS = frozenset({"source", "target", "target_unsourced", "target_required"})


# --- Fixtures -----------------------------------------------------------------


def _source_schema(extra_declared: int = 0) -> dict:
    """A source schema declaring three mapped fields, one orphan, and `extra_declared` more.

    `extra_declared` exists for the denominator test: the payload's
    `total_source_fields` reads `source_schema["properties"]` (`:1728`), so a
    field the schema declares but no mapping list mentions must still be counted.
    """
    properties = {
        "cust_id": {"type": "string"},
        "cust_nm": {"type": "string"},
        "signup_dt": {"type": "string"},
        "legacy_notes": {"type": "string"},
    }
    for i in range(extra_declared):
        properties[f"declared_only_{i}"] = {"type": "string"}
    return {"type": "object", "properties": properties}


def _target_schema(required: list[str] | None = None) -> dict:
    """A target schema with four declared fields; `phone` is the unsourced one."""
    return {
        "type": "object",
        "properties": {
            "customer_id": {"type": "string"},
            "customer_name": {"type": "string"},
            "signup_date": {"type": "string"},
            "phone": {"type": "string"},
        },
        "required": ["customer_id", "phone"] if required is None else required,
    }


def _mapping_result() -> dict:
    """Three mapped fields, one unmapped source, one unsourced target.

    Every entry carries both `source_field` and `target_field`, which is what
    keeps execution out of the degraded `except` arm — `:1707` and `:1716` index
    those two keys directly.

    `phone` sits in `unmapped_target_fields` (the post-p37-C9 placement), so
    `unsourced_target_field_names` returns it and, because `_target_schema`
    declares it required, `target_required` is non-empty.
    """
    return {
        "mappings": [
            {"source_field": "cust_id", "target_field": "customer_id", "confidence": 0.98, "transformation": "direct"},
            {"source_field": "cust_nm", "target_field": "customer_name", "confidence": 0.94},
            {
                "source_field": "signup_dt",
                "target_field": "signup_date",
                "confidence": 0.90,
                "transformation": "to_date(col('signup_dt'), 'yyyy-MM-dd')",
            },
        ],
        "unmapped_source_fields": ["legacy_notes"],
        "unmapped_target_fields": ["phone"],
        "mapping_summary": "Three direct-ish mappings; phone has no source.",
        "generated_by": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    }


def _build(
    *,
    source_schema: dict | None = None,
    target_schema: dict | None = None,
    mapping_result: dict | None = None,
    source_schema_hash: str = "src-hash-p55-1-2",
    target_schema_hash: str = "tgt-hash-p55-1-2",
) -> dict:
    """Call the function under test and refuse to accept a payload it did not build.

    `logger.error` must never have been called. It appears exactly once in this
    function, at `:1783` inside the blanket `except Exception` (`:1782-1788`), so
    this is what stops the degraded three-key return being recorded here as the
    documented shape of the payload.
    """
    with patch.object(_mod, "logger") as mock_logger:
        payload = _mod.create_approval_payload(
            _source_schema() if source_schema is None else source_schema,
            _target_schema() if target_schema is None else target_schema,
            _mapping_result() if mapping_result is None else mapping_result,
            source_schema_hash,
            target_schema_hash,
        )

        mock_logger.error.assert_not_called()

    assert "error" not in payload, "the degraded except arm ran — this is not the documented payload"
    return payload


# --- R5.8: this is the right function of the two --------------------------------


def test_this_is_the_schema_mapping_function_and_not_create_codes():
    """R5.8, asserted rather than trusted to the module docstring.

    The two same-named functions are distinguishable by signature and by nothing
    else a grep can see. If a later edit points this module at `create_code`'s —
    which is what R5.8 is the risk of — the parameter names below stop matching
    and this fails first, before any assertion about payload content confuses the
    reader with a `KeyError`.
    """
    parameters = list(inspect.signature(_mod.create_approval_payload).parameters)

    assert parameters == [
        "source_schema",
        "target_schema",
        "mapping_result",
        "source_schema_hash",
        "target_schema_hash",
    ], f"this is not schema_mapping's create_approval_payload (:1677); create_code's (:521) takes 8: {parameters}"
    assert _mod.__file__.endswith("agentic_data_ingestion_schema_mapping/lambda_function.py"), _mod.__file__


# --- The documented key set ----------------------------------------------------


def test_payload_carries_every_key_the_approval_read_path_documents():
    """The seven producer keys of `tests/fixtures/conftest.py:636-643`, plus the type tag.

    Asserted as a **subset**, not as equality, and the reason is not laziness:
    task 6.2 adds the Rename_Manifest key to this payload deliberately, so an
    equality assertion here would fail for the intended change and would be
    pinning a set already scheduled to grow. What must not happen is a key going
    **missing** — `store_mapping_result:1927` stores this dict whole at
    `output_data.approval`, and every absent key becomes a hole in what an
    approver is shown.
    """
    payload = _build()

    missing = _DOCUMENTED_CONSUMED_KEYS - set(payload)
    assert missing == set(), f"keys the approval read path documents as this producer's, absent: {sorted(missing)}"

    # The type tag is what the read path dispatches an artifact renderer on, and
    # it is the one key present on BOTH arms of this function (`:1733`, `:1785`).
    assert payload["approval_type"] == "schema_mapping"


def test_evidence_and_questions_are_not_this_producers_keys():
    """The producer boundary, documented at `test_schema_mapping_payload_read.py:306-328`.

    Those two keys are written by the complex-mapping agent's top-level
    `update_item`, so a reader that finds them under `output_data.approval` is
    reading a payload this function did not build. Pinning their absence is what
    keeps that read correct while W4 adds a key here.
    """
    payload = _build()

    for key in _KEYS_THIS_PRODUCER_MUST_NOT_OWN:
        assert key not in payload, (
            f"{key} is not part of create_approval_payload's output — its writer is the complex-mapping "
            f"agent's top-level update_item. Adding it here would hide that the top-level read is correct for it."
        )


def test_the_two_schema_hashes_are_carried_through_verbatim():
    """Both hashes reach the payload unchanged (`:1736-1737`).

    Expectation from the parameter names, not from output: a value named
    `source_schema_hash` that arrives at a key named `source_schema_hash` has
    been transformed by nothing. It matters because the p55 reuse lookup (task
    6.3) decides eligibility on Schema_Hash **equality**, and `LGR-b2f79ef-11`
    (template / ratified) already records what a substituted-in placeholder hash
    costs: "it records a missing hash as a valid-looking identity that matches
    nothing, so a hash comparison downstream silently answers 'different' for a
    reason unrelated to the schemas."
    """
    payload = _build(source_schema_hash="src-abc123", target_schema_hash="tgt-def456")

    assert payload["source_schema_hash"] == "src-abc123"
    assert payload["target_schema_hash"] == "tgt-def456"


def test_unmapped_fields_carries_exactly_the_four_documented_keys():
    """The four keys of the docstring's p37 R3.2 block (`:1682-1694`), and their kinds.

    Equality **is** asserted on this sub-dict, unlike the top level: p37's
    contract test already treats these four as the complete set
    (`test_unsourced_keys_contract.py:6-9`, which raises rather than skips on an
    unreadable literal), and W4's key goes at the top level, not in here. A fifth
    key appearing here would mean the unsourced vocabulary grew, which is
    `LGR-9e76249-7`'s subject and not something to absorb silently.

    Kinds, from R3.2's "names, not counts": `target_unsourced` and
    `target_required` are lists of field-name strings. `source` and `target` are
    the caller's own lists, passed through — they may contain dicts, which is why
    they are asserted as pass-through rather than as name lists.
    """
    mapping_result = _mapping_result()
    payload = _build(mapping_result=mapping_result)
    unmapped = payload["unmapped_fields"]

    assert set(unmapped) == _DOCUMENTED_UNMAPPED_FIELDS_KEYS

    assert unmapped["source"] == mapping_result["unmapped_source_fields"]
    assert unmapped["target"] == mapping_result["unmapped_target_fields"]

    assert unmapped["target_unsourced"] == ["phone"]
    assert unmapped["target_required"] == ["phone"]
    for key in ("target_unsourced", "target_required"):
        assert all(isinstance(name, str) for name in unmapped[key]), (
            f"R3.2: {key} NAMES the fields; a non-string member means it is carrying something else"
        )

    # The required subset is a subset of the unsourced set, from the same single
    # reader (`:1700-1702`) — `LGR-9e76249-7`'s no-drift property, read off the
    # payload instead of off `unsourced_target_field_names`.
    assert set(unmapped["target_required"]) <= set(unmapped["target_unsourced"])


def test_mapping_statistics_cannot_contradict_the_lists_the_same_payload_carries():
    """Each statistic means what its name says, measured against this payload's own lists.

    Expectations from the names plus the derivations written beside the fixture at
    `tests/fixtures/conftest.py:857-867`. The point is internal consistency: an
    approver sees the counts and the lists on one screen, so a statistic that
    disagrees with the list beside it is a defect no matter which number is
    "right".
    """
    payload = _build()
    stats = payload["mapping_statistics"]

    assert stats["mapped_fields"] == len(payload["proposed_mappings"]) == 3
    assert stats["unmapped_source_fields"] == len(payload["unmapped_fields"]["source"]) == 1
    assert stats["unmapped_target_fields"] == len(payload["unmapped_fields"]["target"]) == 1
    assert stats["total_target_fields"] == len(_target_schema()["properties"]) == 4

    # `average_confidence` is the mean of `confidence_scores`, rounded to three
    # places (`:1727`, `:1755`). Every target field here is distinct, so the mean
    # over `confidence_scores` and the mean over `proposed_mappings` coincide and
    # the assertion is not sensitive to which reading is taken.
    scores = payload["confidence_scores"]
    assert set(scores) == {"customer_id", "customer_name", "signup_date"}
    assert stats["average_confidence"] == round(sum(scores.values()) / len(scores), 3)

    # `mapping_coverage_percent` — `round(mapped / total_target * 100, 1)`, the
    # derivation the fixture comment states.
    assert stats["mapping_coverage_percent"] == round(3 / 4 * 100, 1)


def test_total_source_fields_counts_the_schemas_declared_fields_not_the_mapping_lists():
    """The payload's source denominator is the source SCHEMA (`:1728`), not the mapping lists.

    This is worth its own test because one module spells two different
    quantities `total_source_fields`, a hundred lines apart, and both are read:

    * `determine_approval_requirement:1625` — `len(mappings) + len(unmapped_source)`,
      the denominator of the `> 0.2` clause at `:1650`.
    * `create_approval_payload:1728` — `len(source_schema["properties"])`, the
      number the approver is shown.

    They agree only when every declared source field appears in one of the two
    lists. The fixture below declares six and lists four, so the two readings
    differ by two, and the payload takes the schema-declared one. W5 (task 3.1)
    computes a Source_Field_Mappability_Ratio and its refusal message names the
    ratio (R14.2), so which denominator it adopts has to be a decision rather
    than an inheritance — this test is what makes the existing pair visible
    instead of assumed.
    """
    payload = _build(source_schema=_source_schema(extra_declared=2))
    stats = payload["mapping_statistics"]

    mapping_list_total = len(payload["proposed_mappings"]) + len(payload["unmapped_fields"]["source"])
    assert mapping_list_total == 4

    assert stats["total_source_fields"] == 6, (
        "the payload counts the fields the source schema DECLARES (:1728); "
        "determine_approval_requirement (:1625) counts mapping-list membership instead"
    )
    assert stats["total_source_fields"] != mapping_list_total
