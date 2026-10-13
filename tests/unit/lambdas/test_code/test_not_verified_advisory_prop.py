# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Feature: p34-code-approval-gate-honors-config, Property 4: Not_Verified_Checks are advisory — the decision is invariant under them
"""Property 4 — Not_Verified_Checks are advisory: the decision is invariant.

*For any* checks list and *any* set of Not_Verified_Check entries injected into
it, the Approval_Policy decision and the Quality_Verdict_Field are both
unchanged from their values on the list without those entries.

**Validates: Requirements 2.6**

Metamorphic, not exemplary
--------------------------
Design §6 chose the metamorphic form because "advisory" cannot be written as an
example without privileging one entry shape. There are three not-verified
producers in `test_code` today — a pandas-proxy refusal
(`NOT_VERIFIED_CHECK_NAME`, `lambda_function.py:413-446`), a test-harness frame
refusal (`NOT_VERIFIED_BY_HARNESS_CHECK_NAME`, `:447-486`) and a
schema-compliance entry with no target schema (`SCHEMA_COMPLIANCE_CHECK_NAME`
with `NO_TARGET_SCHEMA_REASON`, `:516-539`) — and nothing stops a fourth. An
example pins whichever one it names; this pins the *discriminator*, which is
what the three have in common and what a fourth would inherit.

The machinery, read end-to-end before anything was asserted
-----------------------------------------------------------
`requirements.md` §"Current state" states `schema_compliance` is advisory *by
construction*. That was verified rather than assumed, at these lines of
`lambda/agentic_data_ingestion_test_code/lambda_function.py` (anchors as read
while writing this file; `requirements.md`'s own anchors for the same code —
`:515-538` and `:571-572` — predate p34's insertions above them):

* `:215-223` — `is_not_verified_check` is the whole discriminator:
  `check.get("verified") is False`. Not the name, not `passed`, not
  `not_verified_reason`. Its own docstring says why a boolean `passed` cannot
  express three states.
* `:516-539` — the `schema_compliance` entry emitted when `target_schema is
  None`: `verified: False` plus `not_verified_reason: "no_target_schema"`
  (`:537`), and `passed: False`, *replacing* the vacuous 100 rather than
  reporting it.
* `:566-573` — the partition, **before** the `all()`:
  `verified_checks = [c for c in checks if not is_not_verified_check(c)]`
  (`:571`) and
  `test_status = "passed" if verified_checks and all(...) else "failed"`
  (`:572`). Note the `verified_checks and` guard: a list in which *nothing* was
  verified is `"failed"`, and injection cannot reach that guard either, because
  injecting not-verified entries never adds to `verified_checks`.
* `:1635-1641` — the handler's second partition, over the same predicate, from
  which `checks_run = len(verified_checks)` and
  `checks_passed = checks_run - len(failed_check_names)` are derived.
* `:748-817` — `determine_code_approval_requirement`, which consumes
  `(test_status, checks_run, checks_passed, ...)` and never sees `checks`.

So the chain from a checks list to a decision is
`checks → partition → (test_status, checks_run, checks_passed) → gate → policy`,
and every step of it reads the list only through that one predicate. The
property below is that chain, quantified over injections.

Which surface "the Approval_Policy decision" is asserted against, and why
------------------------------------------------------------------------
The Approval_Policy is asserted as **R2.4's disjunction over the two production
helpers that exist**: `resolve_code_approval_setting` (`:702-745`, added by task
2.1) for the Code_Approval_Setting, `determine_code_approval_requirement`
(`:748`) for the Code_Quality_Gate outcome, and `regeneration_attempt > 0` for
R2.3's third input — combined by `any(...)`, which is R2.4 verbatim: "SHALL
require approval when at least one of its three inputs … requires approval, and
SHALL report that approval is not required only when none of the three requires
it."

`determine_merged_approval_requirement` is deliberately **not** imported. It is
being added by task 2.2 concurrently with this file, so importing it would make
this test's result depend on the state of an in-progress edit; and R2.4 defines
the policy as exactly this disjunction, so the model *is* the requirement rather
than a paraphrase of an implementation. Task 2.2's Property 2 is what asserts
that the shipped helper equals the disjunction; this property asserts that the
disjunction is invariant under injection. Composed, they give R2.6 over the
shipped function without either test depending on the other's file.

Generating the two configuration inputs is what makes the invariance
non-vacuous. Two of the policy's three inputs are structurally blind to the
checks list — that is *why* R2.6 holds — so the interesting cases are the ones
where the quality gate is the only input that could change the answer, and those
are only reached by varying the other two.

Why the mirrored derivation is honest, and how it is anchored
------------------------------------------------------------
The partition-and-derive step is inline production code at the two sites cited
above, not an extracted helper, so `_derive_from_checks` below mirrors it. It
mirrors it using the **production** `is_not_verified_check`, so the discriminator
under test is never re-implemented. And
`test_the_partition_agrees_with_the_real_aggregation_on_an_injected_entry`
anchors the mirror against the real `aggregate_check_results` on the one
injection that function supports cleanly — the presence or absence of a
`schema_compliance` key in `data_quality_results` under `target_schema=None`,
which appends exactly one not-verified entry and changes nothing else.

Expectations derive from R2.6, not from current output
------------------------------------------------------
Two places where this property asserts something stronger than the current
producers happen to emit, deliberately:

* every not-verified entry produced today carries `passed: False`, and this
  generator draws `passed` freely. R2.6 says the policy derives **no part** of
  its decision from such an entry, so a `passed: True` not-verified entry must
  not change the answer either. If the derivation ever read `passed` before the
  partition, that is the shape that would catch it.
* a fourth not-verified name is generated alongside the three real ones, because
  R2.6 is about the state, not the vocabulary (`:246`: "a second reason, not a
  third state").

What this does NOT assert, and where that lives instead
-------------------------------------------------------
Nothing here asserts what the decision *is* — only that injection does not move
it. A refusal-only run's absolute answer is p32 2.21's subject and is already
pinned by example: `test_honest_aggregation.py:540`
(`test_proxy_refusal_is_a_not_verified_check_not_a_failed_execution`), `:564`
(`test_refusal_only_run_does_not_trigger_regeneration_or_require_approval`),
`:652` (`test_a_run_with_nothing_verifiable_is_not_reported_as_passed`) and
`:662`, and by `test_not_verified_classification_prop.py`'s property over the
real converter. Those go through the real refusal path for one entry shape;
this generalises over shapes without duplicating them. Design §4.7 row 4 and
task 4.1 own the amendment of `test_honest_aggregation.py:679-680` — this file
does not touch it.

Ledger provenance
-----------------
No record governs this gate; `requirements.md` §"Ledger provenance for this
area" is the record. `why` on
`lambda/agentic_data_ingestion_test_code/lambda_function.py` returns
`LGR-60c4985-1..7` (**template / ratified**, p32) plus the documented snapshot
traps `LGR-0176ccc-1` and `LGR-5feb9b7-1` (**backfilled**). `LGR-60c4985-6`
(**template / ratified**) names `test_honest_aggregation.py:359`, `:399` and
`test_review_gate_calibration.py:478` as held assertions carrying deliberate
reversals; this file adds only, and amends none of them. `LGR-334667e-1`
(**backfilled**) — execution success must not override a static failure — is
respected by construction: the partition removes not-verified entries, never
failing ones, so an injected entry cannot rescue a failed verified check.
`LGR-3e45e65-1` (**template / ratified**) is why this file does not re-pin
`AWS_REGION`: `tests/unit/conftest.py` assigns it before any test module in this
directory is imported.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

# Account id is not pinned by tests/unit/conftest.py (only the region is —
# LGR-3e45e65-1), and env_utils is stubbed below in any case.
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# review_verdict is a real module on the same path. The verdict is held FIXED
# across each metamorphic pair, so what it computes is irrelevant to the
# invariance — but the shapes fed to it are the producer's real ones.
from review_verdict import (
    ATTRIBUTION_SOURCE_DEFAULT,
    ATTRIBUTION_SOURCE_REVIEWER,
    ATTRIBUTION_TEMPLATE_SCAFFOLDING,
    ATTRIBUTION_TRANSFORM_BLOCK,
    VERDICT_FAIL,
    VERDICT_PASS,
)

# Stub heavy / AWS-touching imports so the module loads in isolation. Same set
# as the sibling files in this directory; the module-level cache pop is the
# house pattern for lambda_function cross-contamination (several Lambda dirs
# expose a module of that name).
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(initialize_strands_agent_with_mcp=None),
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(write_agent_audit_entry=None),
    "prompt_template_loader": MagicMock(load_prompt_template=None),
    "metrics_facade": MagicMock(PipelineMetrics=None),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "model_config": MagicMock(resolve_model_id=MagicMock(return_value="model-id")),
    "code_interpreter_runner": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
}

with patch.dict("sys.modules", _mods), patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Several Lambda dirs expose a module named ``lambda_function``."""
    sys.modules["lambda_function"] = _mod
    yield


# ---------------------------------------------------------------------------
# The derivation under test — production predicate, mirrored partition
# ---------------------------------------------------------------------------


def _derive_from_checks(checks: list[dict]) -> tuple[str, int, int]:
    """``(test_status, checks_run, checks_passed)`` exactly as the Lambda derives them.

    Mirrors two inline production sites, which is why they are cited rather than
    imported — neither is an extracted helper:

    * ``lambda_function.py:571-572`` (inside ``aggregate_check_results``) — the
      partition and the ``all()``, including the ``verified_checks and`` guard
      that keeps an all-not-verified list at ``"failed"``.
    * ``lambda_function.py:1635-1641`` (the handler) — the same partition, then
      ``checks_run`` / ``checks_passed`` off the verified subset only.

    The discriminator is **not** mirrored: ``_mod.is_not_verified_check`` is the
    production function, so a change to it changes this derivation too.
    """
    verified = [check for check in checks if not _mod.is_not_verified_check(check)]
    failed = [check for check in verified if not check["passed"]]
    test_status = "passed" if verified and not failed else "failed"
    checks_run = len(verified)
    checks_passed = checks_run - len(failed)
    return test_status, checks_run, checks_passed


def _quality_verdict_field(checks: list[dict], case: dict) -> bool:
    """The Quality_Verdict_Field (design §4.3): the gate's own answer, unmerged."""
    test_status, checks_run, checks_passed = _derive_from_checks(checks)
    return _mod.determine_code_approval_requirement(
        test_status=test_status,
        checks_run=checks_run,
        checks_passed=checks_passed,
        review_verdict=case["review_verdict"],
        regeneration_analysis=case["regeneration_analysis"],
    )


def _approval_policy(checks: list[dict], case: dict) -> tuple[bool, dict]:
    """R2.4's disjunction over its three inputs, plus the per-input map.

    Two of the three inputs cannot see ``checks`` at all — which is the reason
    R2.6 holds — so they are computed from the generated pipeline record and
    regeneration attempt, held fixed across each metamorphic pair. See the
    module docstring for why ``determine_merged_approval_requirement`` is not
    imported here.
    """
    setting, _source = _mod.resolve_code_approval_setting(case["pipeline_metadata"])
    inputs = {
        # R1.1/R1.3: the setting requires approval unless it evaluates to False.
        "pipeline_config": setting,
        "quality_gate": _quality_verdict_field(checks, case),
        # R2.3: a regenerated run is always reviewed.
        "regeneration": case["regeneration_attempt"] > 0,
    }
    return any(inputs.values()), inputs


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

#: Check names the real producer emits (`:276-573`). Nothing in the derivation
#: keys on the name, so these exist to keep the generated lists recognisable as
#: the producer's output rather than arbitrary dicts.
_VERIFIED_CHECK_NAMES = [
    "ai_review_verdict",
    "static_analysis:Correctness",
    "static_analysis:Security",
    "static_analysis:Schema Compliance",
    "code_interpreter_execution",
    "schema_compliance",
]

#: The three not-verified names in the tree today, plus one that does not exist.
#: R2.6 is a statement about the *state* (`verified: False`), not the
#: vocabulary — `:246` records that a new reason is "a second reason, not a
#: third state" — so a fourth producer must inherit the invariance.
_NOT_VERIFIED_NAMES = [
    "not_verified_by_proxy",
    "not_verified_by_harness",
    "schema_compliance",
    "not_verified_by_some_future_producer",
]

#: `not_verified_reason` values the producers emit (`:421`, `:455`, `:235`),
#: plus the absent case: the reason is documentation for a human, and the
#: partition never reads it.
#:
#: p40 task 8.3 added the SIXTH value, `scalar_literal_receiver` — the pandas
#: proxy's receiver-shape fence (p40 R4). It is bound to the PRODUCTION constant
#: rather than transcribed, unlike the five above, for two reasons: a
#: transcription cannot detect the producer renaming it (`LGR-28b4d75-5`,
#: **template / ratified**), and reading the constant off the module under test
#: is what makes the generated value non-vacuously the real one.
#:
#: This is what carries R2.6 onto the new reason: under p40's ratified arm the
#: proxy refuses MORE often than before, so the reason that is about to become the
#: most common not-verified reason is the one the advisory-invariance property most
#: needs to be quantified over. The property is unchanged — only the input space
#: grew, which is the point: R2.6 is about the STATE (`verified: False`), not the
#: vocabulary (`lambda_function.py:264`, "a second reason, not a third state").
#:
#: NOT task 12.3's work. That task extends a DIFFERENT generator — the D2 fence in
#: `test_schema_read_preservation_prop.py:723`, which is about a `verified: False`
#: entry never becoming the ONLY check (`LGR-b2f79ef-2`'s caveat). This list feeds
#: the advisory-invariance property, and `_cases` always draws a base list
#: alongside the injection, so the fence is not exercised here either way.
_NOT_VERIFIED_REASONS = [
    "not_yet_expressible",
    "permanently_inexpressible",
    "test_frame_truncated",
    "no_source_schema",
    "no_target_schema",
    _mod.SCALAR_LITERAL_RECEIVER_REASON,
]


@st.composite
def _verified_check(draw) -> dict:
    """A check that verified something: no ``verified`` key at all.

    Production emits `{name, passed, detail}` for these and adds `verified`
    ONLY to not-verified entries (`:216-222`), so the absence of the key is the
    shape, not an omission.
    """
    return {
        "name": draw(st.sampled_from(_VERIFIED_CHECK_NAMES)),
        "passed": draw(st.booleans()),
        "detail": draw(st.text(max_size=30)),
    }


@st.composite
def _not_verified_entry(draw) -> dict:
    """A Not_Verified_Check: ``verified: False``, everything else free.

    ``passed`` is drawn rather than fixed to ``False``. Every producer sets it
    to ``False`` today, but R2.6 says the policy derives no part of its decision
    from such an entry, so a ``passed: True`` one must not change the answer
    either — and that is the shape that would catch a derivation reading
    ``passed`` before the partition.
    """
    entry = {
        "name": draw(st.sampled_from(_NOT_VERIFIED_NAMES)),
        "passed": draw(st.booleans()),
        "detail": draw(st.text(max_size=30)),
        "verified": False,
    }
    reason = draw(st.one_of(st.none(), st.sampled_from(_NOT_VERIFIED_REASONS)))
    if reason is not None:
        entry["not_verified_reason"] = reason
    return entry


@st.composite
def _review_verdict(draw) -> dict:
    """A canonical `parse_review_verdict` output shape.

    Held fixed across each metamorphic pair, so its Effective_Verdict is not
    computed here — Property 3 owns the verdict axis.
    """
    attribution = draw(
        st.sampled_from([None, ATTRIBUTION_TRANSFORM_BLOCK, ATTRIBUTION_TEMPLATE_SCAFFOLDING]),
    )
    source = draw(st.sampled_from([None, ATTRIBUTION_SOURCE_REVIEWER, ATTRIBUTION_SOURCE_DEFAULT]))
    issue: dict = {"severity": draw(st.sampled_from(["error", "major", "minor"])), "description": "generated issue"}
    if attribution is not None:
        issue["attribution"] = attribution
    if source is not None:
        issue["attribution_source"] = source
    return {
        "verdict": draw(st.sampled_from([VERDICT_PASS, VERDICT_FAIL])),
        "issues": draw(st.lists(st.just(issue), max_size=2)),
        "categories": {},
    }


@st.composite
def _pipeline_records(draw) -> dict:
    """The four record shapes R1.2 / R1.3 / R1.4 distinguish.

    Generated so the Approval_Policy's ``pipeline_config`` input takes **both**
    values: pinned ``True``, the disjunction would be constant and the
    invariance vacuous. Only ``explicit_false`` yields ``False`` — which is
    Property 1's subject, asserted there, not here.
    """
    shape = draw(st.sampled_from(["explicit_false", "explicit_true", "absent", "malformed"]))
    record: dict = {"pipeline_id": "pipe-1"}
    if shape == "explicit_false":
        record["approval_config"] = {"code_generation_enabled": False}
    elif shape == "explicit_true":
        record["approval_config"] = {"code_generation_enabled": True}
    elif shape == "malformed":
        record["approval_config"] = {"code_generation_enabled": draw(st.sampled_from(["false", 0, None]))}
    # "absent" leaves approval_config off entirely — the pre-change record
    # shape (R7.1), which R1.2 resolves as True.
    return record


@st.composite
def _cases(draw, *, min_injected: int = 1) -> dict:
    """A base checks list, an injection, and every input held fixed across the pair.

    ``min_injected`` defaults to 1. The empty injection is excluded not because
    the property does not cover it but because it is the **identity**: with no
    entries injected the two lists are equal, so both sides of every assertion
    are literally the same computation and the example asserts nothing. Forcing
    at least one entry is strictly stronger, and it is what makes each example
    exercise the assertion rather than the fraction that happened to inject
    something. The base list is still allowed to carry not-verified entries of
    its own, so "invariant on top of an existing refusal" stays covered.
    """
    base = draw(st.lists(_verified_check(), max_size=6))
    # An all-passing base is drawn deliberately often. It is the case where an
    # injected entry COULD move the status — with `passed: False` on the entry
    # and every verified check passing, a partition that failed to remove it
    # derives "failed" instead of "passed", and the decision follows. Left to
    # chance, a per-check coin flip over up to six checks makes that case rare,
    # so the property would be quantified mostly over lists that were already
    # "failed" and could not move.
    base_all_passing = draw(st.booleans())
    if base_all_passing:
        base = [{**check, "passed": True} for check in base]
    # The base list may itself already carry not-verified entries: injection has
    # to be invariant on top of an existing refusal, not only on a clean list.
    base += draw(st.lists(_not_verified_entry(), max_size=2))
    base = draw(st.permutations(base)) if base else []

    injected_entries = draw(st.lists(_not_verified_entry(), min_size=min_injected, max_size=3))
    # Arbitrary positions, so the property covers interleaving rather than only
    # appending — the real producers append, but the partition is order-blind
    # and asserting that is free.
    injected = list(base)
    for entry in injected_entries:
        injected.insert(draw(st.integers(min_value=0, max_value=len(injected))), entry)

    regeneration_attempt = draw(st.integers(min_value=0, max_value=3))
    max_attempts = draw(st.integers(min_value=1, max_value=4))
    return {
        "base": list(base),
        "injected": injected,
        "injected_entries": injected_entries,
        "base_all_passing": base_all_passing,
        "review_verdict": draw(_review_verdict()),
        "regeneration_analysis": {
            "regeneration_needed": draw(st.booleans()),
            "max_attempts_reached": regeneration_attempt >= max_attempts,
            "regeneration_attempt": regeneration_attempt,
            "max_attempts": max_attempts,
        },
        "regeneration_attempt": regeneration_attempt,
        "pipeline_metadata": draw(_pipeline_records()),
    }


# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(case=_cases())
def test_the_decision_is_invariant_under_injected_not_verified_checks(case):
    """Property 4 — both fields are unchanged by any injection of any shape.

    Validates: Requirements 2.6
    """
    base, injected, entries = case["base"], case["injected"], case["injected_entries"]

    # Guard: the two lists differ by exactly the injected entries, and every
    # injected entry really is a Not_Verified_Check by the production
    # discriminator. Without this the property could pass by generating nothing.
    assert entries, "guard: min_injected=1 drew no entry, so this example would assert the identity"
    assert len(injected) == len(base) + len(entries)
    for entry in entries:
        assert _mod.is_not_verified_check(entry) is True, (
            f"generated entry {entry!r} is not a Not_Verified_Check by the production discriminator "
            "(lambda_function.py:215-223) — the property would be quantified over the wrong space"
        )

    event(f"injected {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}")
    # p40 task 8.3: observable coverage of the sixth reason, so "the property now
    # covers scalar_literal_receiver" is something `--hypothesis-show-statistics`
    # shows rather than something this comment claims.
    if any(entry.get("not_verified_reason") == _mod.SCALAR_LITERAL_RECEIVER_REASON for entry in entries):
        event("injection carried p40's scalar_literal_receiver reason")
    event(f"base carried {sum(1 for c in base if _mod.is_not_verified_check(c))} not-verified entr(y/ies) already")
    if case["base_all_passing"]:
        event("base is all-passing — an injected entry COULD move the status here")

    base_derived = _derive_from_checks(base)
    injected_derived = _derive_from_checks(injected)
    # The mechanism, asserted first so a failure localises: the derived triple
    # is what the gate consumes, and injection must not move it (`:570-573`,
    # `:1563-1570`).
    assert injected_derived == base_derived, (
        f"injecting {len(entries)} not-verified entr(y/ies) changed the derived "
        f"(test_status, checks_run, checks_passed) from {base_derived} to {injected_derived} — "
        "the partition is no longer removing them before the all(). R2.6"
    )
    event(f"derived test_status: {base_derived[0]}")
    if base_derived[1] == 0:
        event("nothing verified — the `verified_checks and` guard at :572 holds the status at failed")

    # R3.1's field: the Code_Quality_Gate's own answer, unmerged.
    base_quality = _quality_verdict_field(base, case)
    injected_quality = _quality_verdict_field(injected, case)
    assert injected_quality is base_quality, (
        f"the Quality_Verdict_Field moved from {base_quality} to {injected_quality} when "
        f"{len(entries)} not-verified entr(y/ies) were injected — R2.6"
    )

    # R2.4's disjunction: the Approval_Policy decision, and the per-input map,
    # both invariant. Asserting the map as well as the boolean means an
    # injection cannot change *which* input required approval while leaving the
    # answer alone — that would still be a decision derived from a
    # Not_Verified_Check (R3.5's audit row would then be wrong).
    base_decision, base_inputs = _approval_policy(base, case)
    injected_decision, injected_inputs = _approval_policy(injected, case)
    event(f"policy inputs: {sorted(k for k, v in base_inputs.items() if v)}")
    assert injected_decision is base_decision, (
        f"the Approval_Policy decision moved from {base_decision} to {injected_decision} when "
        f"{len(entries)} not-verified entr(y/ies) were injected "
        f"(inputs {base_inputs} → {injected_inputs}) — R2.6"
    )
    assert injected_inputs == base_inputs, (
        f"the Approval_Policy's per-input map moved from {base_inputs} to {injected_inputs} — R2.6/R3.5"
    )


@settings(max_examples=200, deadline=None)
@given(case=_cases())
def test_only_the_verified_key_can_move_the_decision(case):
    """Property 4's contrapositive — the same entries, verified, DO move it.

    Same generated entries, with `verified: False` removed so each becomes an
    ordinary check. This is what makes the property above a statement about the
    discriminator rather than about the entries being ignorable: if dropping the
    key changed nothing either, the first test would be passing because the
    generated entries are inert, not because the partition works.

    One-directional: a verified failing check must move `test_status` to
    `"failed"`; a verified passing check need not move anything. Only the first
    direction is asserted.

    Validates: Requirements 2.6
    """
    base = case["base"]
    entries = case["injected_entries"]
    failing = [{k: v for k, v in entry.items() if k != "verified"} for entry in entries if entry["passed"] is False]
    if not failing:
        event("no failing entry drawn — nothing to assert in this direction")
        return

    as_verified = list(base) + failing
    status, checks_run, _passed = _derive_from_checks(as_verified)
    event(f"{len(failing)} entr(y/ies) re-read as verified failures")

    assert checks_run == len(as_verified) - sum(1 for c in base if _mod.is_not_verified_check(c)), (
        "stripping `verified` did not move the entries into the verified subset — the discriminator "
        "is reading something other than that key (lambda_function.py:215-223)"
    )
    assert status == "failed", (
        f"a failing check with no `verified` key derived {status!r}: the partition is removing entries "
        "it should not, so `verified: False` is not the only thing that makes a check advisory — R2.6"
    )


# ---------------------------------------------------------------------------
# The anchor — the mirrored partition against the real aggregation
# ---------------------------------------------------------------------------

#: A parsed-review payload whose checks all pass, so the emitted list is
#: recognisable and the injected entry is the only non-passing thing in it.
_ALL_STATIC_PASS = {
    "verdict": VERDICT_PASS,
    "issues": [],
    "test_categories": {
        "Correctness": {"status": "PASSED", "passed": True},
        "Security": {"status": "PASSED", "passed": True},
    },
}


@settings(max_examples=150, deadline=None)
@given(
    score=st.integers(min_value=0, max_value=100),
    missing=st.lists(st.text(min_size=1, max_size=6), max_size=3),
    input_records=st.integers(min_value=0, max_value=100),
)
def test_the_partition_agrees_with_the_real_aggregation_on_an_injected_entry(score, missing, input_records):
    """Property 4 through `aggregate_check_results` itself, for the entry it emits.

    The one injection the real function supports cleanly: with
    `target_schema=None` held constant, a `schema_compliance` key inside
    `data_quality_results` appends exactly one not-verified entry
    (`lambda_function.py:517-539`) and changes nothing else, while its absence
    appends nothing (`:516`'s `if SCHEMA_COMPLIANCE_CHECK_NAME in
    quality_results` gate). So the two calls differ by an injection and by
    nothing else.

    This is what keeps `_derive_from_checks` honest: the mirror is asserted to
    agree with the real derivation on the entry the real producer emits, for
    every score — including the vacuous 100 that requirement p33 2.6 exists to
    stop being reported as a pass.

    Validates: Requirements 2.6
    """
    execution = {
        "execution_successful": True,
        "input_records": input_records,
        "output_records": input_records,
        "data_quality_results": {},
    }
    base_status, base_checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, execution, None)
    # Non-vacuity: the base run passes, and the injected entry carries
    # `passed: False`, so a partition that failed to remove it would derive
    # "failed". The invariance below is therefore over a status that WOULD flip,
    # not one that was already "failed" for an unrelated reason.
    assert base_status == "passed", f"guard: the base run is {base_status!r}, so the invariance would be trivial"

    with_entry = dict(execution)
    with_entry["data_quality_results"] = {"schema_compliance": score, "missing_columns": list(missing)}
    injected_status, injected_checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, with_entry, None)

    # The two runs differ by exactly one appended entry, and it is not-verified.
    assert injected_checks[: len(base_checks)] == base_checks
    assert len(injected_checks) == len(base_checks) + 1
    entry = injected_checks[-1]
    assert _mod.is_not_verified_check(entry) is True, f"the emitted schema_compliance entry is verified: {entry!r}"
    assert entry["not_verified_reason"] == _mod.NO_TARGET_SCHEMA_REASON

    # The real aggregation's status is invariant under it, and the mirrored
    # derivation agrees with the real one on both lists.
    assert injected_status == base_status, (
        f"the real aggregation moved from {base_status!r} to {injected_status!r} when its own "
        "not-verified schema_compliance entry appeared — R2.6"
    )
    assert _derive_from_checks(base_checks)[0] == base_status, (
        "_derive_from_checks disagrees with aggregate_check_results on the base list — the mirror of "
        "lambda_function.py:570-573 has drifted from the production derivation"
    )
    assert _derive_from_checks(injected_checks)[0] == injected_status, (
        "_derive_from_checks disagrees with aggregate_check_results on the injected list"
    )


# ── p40 task 8.3 — the sixth reason, pinned deterministically ────────────────
#
# WHY THIS EXISTS ALONGSIDE THE GENERATOR CHANGE. Adding
# `scalar_literal_receiver` to `_NOT_VERIFIED_REASONS` widens the property's input
# space, which is what R2.6 needs — but the reason is drawn through
# `st.one_of(st.none(), st.sampled_from(...))`, and MEASURED coverage of the new
# value is only about 2% of examples (`--hypothesis-show-statistics`, the
# "injection carried p40's scalar_literal_receiver reason" event). 2% is real
# coverage but it is thin, and "the property now covers the new reason" should not
# rest on it. This test makes the same three surfaces certain for that one reason.
#
# It is NOT a duplicate of `test_not_verified_classification_prop.py`. That file
# drives the REAL converter → runner → `aggregate_check_results` path and asserts
# the absolute answers. This one asserts THIS file's subject — the metamorphic
# invariance, and the Approval_Policy disjunction including
# `resolve_code_approval_setting`, which the classification property never calls.


def test_the_scalar_literal_reason_is_advisory_on_all_three_surfaces():
    """p40 R4/R5 through R2.6's own surfaces, for the reason p40 adds.

    The base list is all-passing and the injected entry carries `passed: False`,
    so a partition that failed to remove it would derive `"failed"` and the gate
    would follow — the invariance below is over an answer that COULD move, not one
    that was already `"failed"` for an unrelated reason.

    Validates: Requirements 2.6; p40 Requirements R4, R5
    """
    base = [
        {"name": "ai_review_verdict", "passed": True, "detail": "pass"},
        {"name": "static_analysis:Correctness", "passed": True, "detail": "PASSED"},
    ]
    # The real producer's shape for a p40 refusal: `not_verified_by_proxy`,
    # `passed: False`, `verified: False`, and the new reason — bound to the
    # production constant, not transcribed.
    entry = {
        "name": "not_verified_by_proxy",
        "passed": False,
        "detail": "NOT VERIFIED by the pandas proxy: construct 'trim' on line 2 was not converted",
        "verified": False,
        "not_verified_reason": _mod.SCALAR_LITERAL_RECEIVER_REASON,
    }
    injected = [*base, entry]

    case = {
        "review_verdict": {"verdict": VERDICT_PASS, "issues": [], "categories": {}},
        "regeneration_analysis": {
            "regeneration_needed": False,
            "max_attempts_reached": False,
            "regeneration_attempt": 0,
            "max_attempts": 2,
        },
        "regeneration_attempt": 0,
        # `explicit_false` is the only shape that lets `pipeline_config` be False,
        # which is what makes the quality gate the deciding input — pinned True,
        # the disjunction is constant and the invariance vacuous (R1.3).
        "pipeline_metadata": {"pipeline_id": "pipe-1", "approval_config": {"code_generation_enabled": False}},
    }

    # Guard: the entry is a Not_Verified_Check by the PRODUCTION discriminator,
    # and the base run really does pass.
    assert _mod.is_not_verified_check(entry) is True
    assert _derive_from_checks(base) == ("passed", 2, 2)

    # Surface 1 — the derivation the gate consumes (`:571-572`, `:1635-1641`).
    assert _derive_from_checks(injected) == _derive_from_checks(base), (
        "a scalar_literal_receiver entry moved (test_status, checks_run, checks_passed) — the partition is "
        "not removing it, so p40's new refusal would fail the run it exists to stop failing"
    )

    # Surface 2 — the Code_Quality_Gate's own answer (the real
    # `determine_code_approval_requirement`). R5: a partitioned-out check reaches
    # it only through `test_status`, which surface 1 just showed is unmoved.
    assert _quality_verdict_field(injected, case) is False
    assert _quality_verdict_field(injected, case) is _quality_verdict_field(base, case)

    # Surface 3 — R2.4's disjunction and its per-input map. Both invariant, so an
    # injection cannot change WHICH input required approval either (R3.5).
    injected_decision, injected_inputs = _approval_policy(injected, case)
    base_decision, base_inputs = _approval_policy(base, case)
    assert injected_decision is base_decision
    assert injected_inputs == base_inputs
    # Non-vacuity: with the setting explicitly False, a pass verdict and no
    # regeneration, every input is False — so the decision WOULD have flipped if
    # the refusal had reached the quality gate.
    assert base_decision is False, f"guard: the base decision is {base_decision}, so the invariance is trivial"
