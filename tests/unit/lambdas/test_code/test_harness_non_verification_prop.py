# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Feature: real-spark-verification, Property 9: A harness non-verification never carries a passing state
"""Property 9 — a harness non-verification never carries a passing state.

*For any* ``not_verified_reason`` a harness non-verification can carry — the
three that ship today, and any string a fourth producer might spell tomorrow —
the check ``aggregate_check_results`` emits carries ``verified`` false **and**
``passed`` false (R8.1, R8.2), is excluded from the Verified_Check_Partition so
that it neither passes nor fails the run (R8.3), and adds **no third check
state** beyond the explicit ``verified: False`` discriminator (R12.3).

**Validates: Requirements 8.1, 8.2, 8.3, 12.3**

Why the reason is the quantified axis
-------------------------------------
There are three harness reasons now. p33 shipped two (``test_frame_truncated``,
``no_source_schema``); p42 task 3.6 added ``session_capacity_exhausted``, and its
worked examples live in ``test_session_capacity_non_verification.py`` (21 tests).
Those examples pin the third reason. This file pins the *shape of the claim*, so
the **fourth** reason is covered the day it is written rather than the day
somebody remembers to add a case here.

That matters because the harness branch of ``aggregate_check_results``
(``lambda_function.py:625-711``) is no longer one sentence: task 3.6 made its
opening clause reason-dependent, because a capacity refusal has no field count
and no ceiling. A branch that chooses copy by reason is a branch where a new
reason can pick up the wrong arm — so the property has to hold across the reason
axis, not at three sampled points on it.

The reasons are DISCOVERED, not listed
--------------------------------------
:func:`_declared_reason_values` AST-scans every module in
``lambda/agentic_data_ingestion_test_code/`` for module-level ``*REASON*``
constants bound to string literals, resolving the deliberate aliases. A reason
added to any producer in that directory therefore joins this property's input
space automatically, with no edit here — which is the task's explicit
instruction and is the same technique, for the same stated reason, as
``tests/contracts/test_not_verified_reason_labels_cross_language.py``.

``ast`` rather than a substring scan follows ``LGR-b46a5bb-4`` (**template /
ratified**), which rejected substring matching for a repository-wide invariant
test because it cannot tell source from prose — and these constants are named in
dozens of comment lines here.

To be exact about what is and is not imported, because the distinction is the
thing that makes the discovery trustworthy:

* **Enumeration** is by ``ast``. Importing cannot enumerate — it would mean
  importing every module in the directory, including ``lambda_function``, which
  needs the stub dict below and would then have to be re-imported per scan.
* **Two specific constants** (``NOT_VERIFIED_REASON_FRAME_TRUNCATED``,
  ``NOT_VERIFIED_REASON_SESSION_CAPACITY``) ARE imported, from producers that
  are safe to import here: both are pulled in *before* the ``patch.dict``, which
  restores ``sys.modules`` on exit, so they are the real modules' real strings.
  :func:`test_the_reason_scan_finds_every_reason_the_shipped_producers_emit`
  asserts they are ``str`` rather than trusting it — a ``MagicMock`` would
  compare unequal to everything and fail loudly, but the explicit guard says so.

The hazard the producers themselves record at ``lambda_function.py:240-253`` and
``glue_session_runner.py:680-688`` is narrower than "never import a constant":
it is that a module which STUBS ``code_interpreter_runner`` cannot then import a
constant *through* it. That is why ``SESSION_CAPACITY_REASON`` is a second
spelling inside ``lambda_function``, and it is why nothing below reaches for a
reason via ``_mod``.

The generator draws from three arms, not one:

* every **declared** reason value (so a new constant is generated automatically),
* **arbitrary** snake-case strings (so a reason not yet declared anywhere is
  covered by construction — this is the half that makes the property a statement
  about the branch rather than about today's vocabulary), and
* the **absent** reason, which exercises the producer-side default at
  ``lambda_function.py:631``.

Anti-vacuity, which is not hypothetical here
--------------------------------------------
Task 3.6 hit this exact trap during development: a **transcribed** reviewer block
produced ZERO checks, which emptied the verified partition, made every status
``"failed"`` for the wrong reason, and left the equality assertion holding
vacuously. So:

* reviewer blocks are built through the shipped ``parse_review_verdict`` →
  ``verdict_to_parsed_results`` path, never transcribed, and always carry at
  least one category;
* every comparison asserts the verified partition is **non-empty** first, with a
  message naming the failure mode;
* :func:`_cases` guarantees at least one all-passing reviewer block shape is
  reachable, so the status compared is one that *could* have moved.

``LGR-b2f79ef-2``'s caveat is the reason the non-empty guard is load-bearing
rather than decoration: a ``verified: False`` entry must never be the only check,
and the partition derives ``"failed"`` from an empty verified set
(``lambda_function.py:709-710``, the ``verified_checks and`` guard). Without the
guard, "excluded from the partition" and "the partition was empty" are
indistinguishable.

Four separate derivations of the discovery are cross-checked, for the same
reason: :func:`test_the_reason_scan_finds_every_reason_the_shipped_producers_emit`
asserts the AST scan is a superset of what the shipped harness producers
*actually emit*, discovered by calling them. A silently degraded scan fails there
instead of quietly shrinking this property's input space to nothing.

Result dicts come from the shipped builders
-------------------------------------------
Nothing below transcribes a result shape. The two harness shapes are produced by
``CodeInterpreterTestRunner._frame_truncation_results`` (the width shape, which
already takes the reason as a parameter) and
``glue_session_runner.session_capacity_results`` (the capacity shape, which
carries neither width key), with only the reason varied on the latter. The
success baseline is ``glue_session_runner._base_results``, the module's own
one-place key set. A producer that renames a key therefore fails these tests
rather than passing them against a stale copy (``LGR-28b4d75-5``, **template /
ratified**: a test written from the producer's output cannot detect the producer
drifting).

What this does NOT assert, deliberately
---------------------------------------
**Copy quality for an unmapped reason.** For a reason the harness branch has no
arm for, the ``else`` renders the width sentence, and with a capacity-shaped
result — which carries no ``not_verified_field_count`` — that reads "declares
None field(s) against a test-frame width ceiling of None". That is a real latent
gap: the cross-language contract test forces a *label* for every new reason but
nothing forces a copy *arm*. It is out of Property 9's scope, which is R8.2's
state and R12.3's absence of a third one — R8/R12.3 say nothing about prose for a
reason that does not exist yet — and asserting it here would paint the suite red
over an input that has not shipped. It is recorded as an observation in the task
report instead. The copy of the three reasons that DO ship is pinned by example
in ``test_session_capacity_non_verification.py``
(``test_capacity_check_detail_names_capacity_and_not_a_frame_width``,
``test_the_width_reasons_copy_is_unchanged``).

**The absolute status of a harness-only run.** That is p32 2.21's subject and is
pinned by example in ``test_honest_aggregation.py``. This property is
metamorphic: injection does not move the status.

**Advisory-invariance of the approval decision.** That is p34's Property 4
(``test_not_verified_advisory_prop.py``), quantified over *injected entry
shapes*. This file is quantified over the *reason a shipped producer emits*, and
goes through the real ``aggregate_check_results`` rather than a mirrored
partition.

Ledger provenance
-----------------
``why`` on ``lambda_function.py:676`` returns only ``LGR-0176ccc-1``
(**backfilled**), the documented 634-file working-tree snapshot that matches
nearly every query in this tree — no fence specific to this seam.
``why`` on ``glue_session_runner.py`` returns nothing at all: the module is new
in this spec and its decisions are still in the spec rather than in the ledger.
Records that did govern choices here:

* ``LGR-b2f79ef-2`` (**template / ratified**) — a third CHECK FIELD was refused
  once already, with the caveat that a ``verified: False`` entry must never be
  the only check. Both halves are asserted: R12.3's key-set equality, and the
  non-empty partition guard.
* ``LGR-b46a5bb-4`` (**template / ratified**) — AST, not substring, for a
  scan over a source tree.
* ``LGR-3e45e65-1`` (**template / ratified**) — ``tests/unit/conftest.py``
  assigns ``AWS_REGION`` before any module in this directory is imported, so the
  ``setdefault`` calls below are consistent no-ops rather than the thing that
  chooses the region.
* ``LGR-69ca78b-1`` (**invariant**) — the governance Choice states fail closed,
  which is why a key no condition reads is inert; it is also why ``test_status``
  is the value R8.3 has to be asserted on.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

# Region/account before any boto3 import. LGR-3e45e65-1 pins the region in
# tests/unit/conftest.py, so these are consistent no-ops.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = Path(__file__).resolve().parents[4] / "lambda" / "agentic_data_ingestion_test_code"
sys.path.insert(0, str(_TEST_CODE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "shared"))

# The REAL producers. Imported before the patch.dict below so sys.modules
# carries the real modules afterwards (patch.dict restores on exit).
from code_interpreter_runner import (
    NOT_VERIFIED_REASON_FRAME_TRUNCATED,
    CodeInterpreterTestRunner,
)
from glue_session_runner import (
    NOT_VERIFIED_REASON_SESSION_CAPACITY,
    _base_results,
    create_session_failure_results,
)

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
# Discovery — the reason vocabulary, read out of the producers
# ---------------------------------------------------------------------------


def _declared_reason_values() -> dict[str, str]:
    """``{"<module>.<CONSTANT>": value}`` for every producer reason constant.

    Module-level assignments in ``lambda/agentic_data_ingestion_test_code/``
    whose name contains ``REASON`` and whose value is a string literal. An
    assignment of that shape bound to a bare name — the deliberate alias
    ``NOT_VERIFIED_REASON_SCALAR_LITERAL = REFUSAL_REASON_SCALAR_LITERAL`` — is
    resolved against the literals rather than dropped, so an alias pointing at
    something this scan cannot see fails loudly instead of silently shrinking
    the vocabulary.

    Scoped to a **directory**, not to an enumerated file list, so a fourth
    producer module is covered the day it appears. See the module docstring for
    why this is ``ast`` and not an import (``LGR-b46a5bb-4``).
    """
    sources = sorted(_TEST_CODE_DIR.glob("*.py"))
    assert sources, f"no producer modules found under {_TEST_CODE_DIR} — the reason constants moved."

    literals: dict[str, str] = {}
    aliases: dict[str, str] = {}

    for path in sources:
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in module.body:
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            else:
                continue
            for name in names:
                if "REASON" not in name:
                    continue
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    literals[f"{path.stem}.{name}"] = node.value.value
                elif isinstance(node.value, ast.Name):
                    aliases[f"{path.stem}.{name}"] = node.value.id

    by_constant_name = {qualified.split(".", 1)[1]: value for qualified, value in literals.items()}
    for qualified, target in aliases.items():
        assert target in by_constant_name, (
            f"{qualified} is bound to {target}, which is not a reason string literal this scan found — "
            "it may be carrying a reason this property would then never generate."
        )
        literals[qualified] = by_constant_name[target]

    assert literals, (
        "no reason constants found in lambda/agentic_data_ingestion_test_code/ — the `*REASON*` naming "
        "convention changed, so this property's generated reason axis would be empty and every "
        "assertion below would be quantified over nothing."
    )
    return literals


#: Every declared reason value, computed once at import. This is the set a new
#: producer constant joins automatically.
DECLARED_REASONS: frozenset[str] = frozenset(_declared_reason_values().values())


def _emitted_harness_reasons() -> dict[str, str]:
    """``{reason: how it was produced}`` for the reasons shipped producers EMIT.

    Discovered by CALLING the producers rather than by reading a list, so this is
    a second, independent derivation of the vocabulary — the corroboration that
    keeps :data:`DECLARED_REASONS` from silently degrading to a partial set.

    Three entry points, which is the whole of the harness surface today:

    * ``_frame_refusal`` on a source schema wider than the ceiling, and on an
      absent one — the two p33 width reasons, read off the shipped classifier
      rather than named here.
    * ``create_session_failure_results`` on a ``ResourceNumberLimitExceededException``
      — p42's capacity reason, through the routing seam rather than through
      ``session_capacity_results`` directly, so the routing is part of what is
      corroborated.
    """
    emitted: dict[str, str] = {}

    wide = {"properties": {f"c{i}": {"type": "string"} for i in range(600)}}
    for label, schema in (("_frame_refusal(wide schema)", wide), ("_frame_refusal(no schema)", {})):
        refusal = CodeInterpreterTestRunner._frame_refusal(schema)
        assert refusal is not None, f"{label} did not refuse — the harness reason it carries is undiscoverable."
        emitted[refusal[0]] = label

    capacity = create_session_failure_results(
        ClientError(
            {"Error": {"Code": "ResourceNumberLimitExceededException", "Message": "limit reached"}},
            "CreateSession",
        )
    )
    assert capacity.get("not_verified_by_harness") is True, (
        "create_session_failure_results no longer routes a capacity refusal to a harness "
        "non-verification (R8.1) — task 3.6's seam has regressed."
    )
    emitted[capacity["not_verified_reason"]] = "create_session_failure_results(capacity)"
    return emitted


# ---------------------------------------------------------------------------
# Result shapes — produced by the shipped builders, never transcribed
# ---------------------------------------------------------------------------

#: The two width keys the capacity shape omits. Read off the CONSUMER's
#: contract constants, which is where the "change both or neither" pair is
#: declared (``lambda_function.py:273-283``).
_WIDTH_KEYS = (_mod.HARNESS_FIELD_COUNT_KEY, _mod.HARNESS_FRAME_CEILING_KEY)


def _width_shaped(reason: str | None, field_count: int, ceiling: int) -> dict:
    """The p33 width shape, from the shipped producer, carrying *reason*.

    ``_frame_truncation_results`` already takes the reason as a parameter, so an
    arbitrary reason travels through the real builder rather than through a dict
    written here. ``reason=None`` removes the key entirely, which is the input
    that exercises the consumer's ``or "test_frame_truncated"`` default.
    """
    results = CodeInterpreterTestRunner._frame_truncation_results(field_count, ceiling, reason or "")
    if reason is None:
        del results["not_verified_reason"]
    else:
        results["not_verified_reason"] = reason
    return results


def _capacity_shaped(reason: str | None) -> dict:
    """The p42 capacity shape — no width keys — carrying *reason*.

    Built from the shipped routing seam and then re-pointed at *reason*, so a
    future harness reason that has no field count and no ceiling is generated
    with the shape such a reason really has. The guard is what makes that claim
    honest rather than assumed.
    """
    results = create_session_failure_results(
        ClientError(
            {"Error": {"Code": "ResourceNumberLimitExceededException", "Message": "limit reached"}},
            "CreateSession",
        )
    )
    assert all(key not in results for key in _WIDTH_KEYS), (
        "the capacity shape has grown a width key, so this arm no longer generates the "
        "'no field count, no ceiling' shape it exists to generate."
    )
    if reason is None:
        results.pop("not_verified_reason", None)
    else:
        results["not_verified_reason"] = reason
    return results


def _executed_successfully() -> dict:
    """A run that DID execute — the R8.3 comparison baseline.

    ``glue_session_runner._base_results`` is the module's own one-place key set
    (its docstring: "One place the key set lives, so no builder below can omit a
    key a report or the gate reads"), so only the values observed here are
    stated. ``data_quality_results`` stays empty on purpose: a non-empty one
    would append a ``schema_compliance`` check and change the partition by
    something other than the injection under test.
    """
    return _base_results(
        execution_successful=True,
        executed=True,
        exit_code=0,
        input_records=100,
        output_records=100,
    )


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

#: An arbitrary future reason: snake_case, and short enough that it cannot be
#: pushed out of the detail by the consumer's ``[:1000]`` truncation.
_ARBITRARY_REASONS = st.from_regex(r"[a-z][a-z0-9_]{0,30}", fullmatch=True)


@st.composite
def _reasons(draw) -> str | None:
    """A reason: declared, arbitrary, or absent.

    ``sorted`` on the declared set so the draw is reproducible across runs
    (a frozenset's iteration order is not).
    """
    arm = draw(st.sampled_from(["declared", "arbitrary", "absent"]))
    if arm == "declared":
        return draw(st.sampled_from(sorted(DECLARED_REASONS)))
    if arm == "arbitrary":
        return draw(_ARBITRARY_REASONS)
    return None


@st.composite
def _harness_results(draw) -> tuple[dict, str | None]:
    """A harness non-verification from a shipped builder, and its reason."""
    reason = draw(_reasons())
    if draw(st.booleans()):
        field_count = draw(st.integers(min_value=0, max_value=5000))
        ceiling = draw(st.integers(min_value=1, max_value=512))
        results = _width_shaped(reason, field_count, ceiling)
    else:
        results = _capacity_shaped(reason)
    assert results.get("not_verified_by_harness") is True, (
        f"generated result is not a harness non-verification: {sorted(results)} — the property would be "
        "quantified over the wrong branch of aggregate_check_results."
    )
    return results, reason


_CATEGORY_NAMES = ["Correctness", "Security", "Schema Compliance", "Performance"]


@st.composite
def _reviewer_blocks(draw) -> dict:
    """A ``parsed_results`` block, built by the shipped parsers.

    ``parse_review_verdict`` → ``verdict_to_parsed_results``, never transcribed:
    a transcribed block produced ZERO checks during task 3.6's development,
    which emptied the verified partition and made every status "failed" for the
    wrong reason. At least one category is always drawn, so the partition is
    never empty for want of an input.

    An **all-passing** block is drawn deliberately often rather than left to a
    per-category coin flip. It is the case where the harness entry COULD move the
    status: with every verified check passing, a partition that failed to remove
    a ``passed: False`` entry derives "failed" instead of "passed". Left to
    chance over up to three categories, a verdict and an issue attribution, that
    case was measured at ~15% of examples, so the property would mostly be
    quantified over runs that were already "failed" and could not move.

    Names are taken off a permutation rather than drawn as a ``unique=True``
    list: the list form rejects duplicates, which showed up as 197 invalid
    examples against 300 valid ones.

    A ``fail`` verdict always carries an issue — ``parse_review_verdict`` rejects
    a failing verdict with nothing to act on as malformed (R13.3).
    """
    count = draw(st.integers(min_value=1, max_value=3))
    names = draw(st.permutations(_CATEGORY_NAMES))[:count]

    all_passing = draw(st.booleans())
    if all_passing:
        raw = {"verdict": "pass", "issues": [], "categories": dict.fromkeys(names, "pass")}
    else:
        verdict_passed = draw(st.booleans())
        issues: list[dict] = []
        if not verdict_passed or draw(st.booleans()):
            issues.append(
                {
                    "severity": draw(st.sampled_from(["error", "major", "minor"])),
                    "description": "generated issue",
                    "attribution": draw(st.sampled_from(["transform_block", "template_scaffolding"])),
                }
            )
        raw = {
            "verdict": "pass" if verdict_passed else "fail",
            "issues": issues,
            "categories": {name: ("pass" if draw(st.booleans()) else "fail") for name in names},
        }
    return _mod.verdict_to_parsed_results(_mod.parse_review_verdict(json.dumps(raw)))


@st.composite
def _cases(draw) -> dict:
    """A harness non-verification, its reason, and a reviewer block."""
    results, reason = draw(_harness_results())
    return {"results": results, "reason": reason, "parsed": draw(_reviewer_blocks())}


# ---------------------------------------------------------------------------
# Helpers over the emitted check list
# ---------------------------------------------------------------------------


def _harness_check(checks: list[dict]) -> dict:
    """The single ``not_verified_by_harness`` entry, asserted to be single."""
    matches = [c for c in checks if c.get("name") == _mod.NOT_VERIFIED_BY_HARNESS_CHECK_NAME]
    assert len(matches) == 1, (
        f"expected exactly one harness check, got {[c.get('name') for c in checks]} — a harness "
        "non-verification must REPLACE the execution check, not join it."
    )
    return matches[0]


def _verified(checks: list[dict]) -> list[dict]:
    """The Verified_Check_Partition, through the production discriminator."""
    return [c for c in checks if not _mod.is_not_verified_check(c)]


def _status_of(verified_checks: list[dict]) -> str:
    """``lambda_function.py:709-710``'s derivation over a given partition.

    Mirrored rather than imported because it is inline production code, and it is
    used only to show that the harness run's status is the one its *reviewer*
    checks alone imply. The discriminator itself is never mirrored.
    """
    return "passed" if verified_checks and all(c["passed"] for c in verified_checks) else "failed"


def _reference_check_keys() -> frozenset[str]:
    """The key set a harness check carries, from the SHIPPED width builder.

    R12.3's oracle. Produced rather than written out, so "no third state" is
    checked against what a harness check really carries and a producer that adds
    a key fails the property instead of agreeing with a stale copy
    (``LGR-28b4d75-5``).
    """
    _status, checks = _mod.aggregate_check_results(
        _mod.verdict_to_parsed_results(
            _mod.parse_review_verdict(json.dumps({"verdict": "pass", "issues": [], "categories": {"Security": "pass"}}))
        ),
        _width_shaped(NOT_VERIFIED_REASON_FRAME_TRUNCATED, 600, 512),
    )
    return frozenset(_harness_check(checks))


#: Computed once rather than per example: the shipped builder plus the real
#: aggregation is not cheap enough to run 300 times for a constant.
_REFERENCE_CHECK_KEYS: frozenset[str] = _reference_check_keys()


# ---------------------------------------------------------------------------
# Discovery guards — run before the property, so a degraded scan fails HERE
# ---------------------------------------------------------------------------


def test_the_reason_scan_finds_every_reason_the_shipped_producers_emit():
    """Two independent derivations of the vocabulary must agree.

    The AST scan is a *superset* claim, not an equality: it also finds the proxy
    reasons, which no harness result carries. What must hold is that nothing the
    shipped harness producers actually emit is missing from it — a scan that
    silently stopped matching would otherwise shrink this property's generated
    reason axis without failing anything.

    Validates: Requirements 8.1
    """
    emitted = _emitted_harness_reasons()

    assert len(emitted) >= 3, (
        f"only {len(emitted)} harness reason(s) discovered from the producers ({sorted(emitted)}); three "
        "ship today (two width reasons plus capacity), so the discovery has degraded."
    )
    missing = {reason: how for reason, how in emitted.items() if reason not in DECLARED_REASONS}
    assert not missing, (
        f"these reasons are EMITTED by a shipped harness producer but were not found by the AST scan: "
        f"{sorted(f'{r!r} via {how}' for r, how in missing.items())}. The scan looks for module-level "
        "`*REASON*` constants bound to string literals; a reason built inline or spelled at its use site "
        "is invisible to it, and this property would then never generate it."
    )
    # The two imported constants are REAL strings, not auto-attribute mocks.
    # A mock would compare unequal to everything and so would fail the two
    # membership assertions below anyway; this says which failure it was.
    for name, value in (
        ("NOT_VERIFIED_REASON_SESSION_CAPACITY", NOT_VERIFIED_REASON_SESSION_CAPACITY),
        ("NOT_VERIFIED_REASON_FRAME_TRUNCATED", NOT_VERIFIED_REASON_FRAME_TRUNCATED),
    ):
        assert isinstance(value, str), (
            f"{name} arrived as {type(value).__name__}, not str — the producer module was stubbed before "
            "this file imported it, so every comparison against it is against a mock"
        )

    # The two named reasons, so a REMOVAL is caught too: the assertions above
    # would still pass if capacity were deleted and two width reasons remained.
    assert NOT_VERIFIED_REASON_SESSION_CAPACITY in emitted, (
        "no shipped producer emits the capacity reason any more — R8.1's routing seam is gone"
    )
    assert NOT_VERIFIED_REASON_FRAME_TRUNCATED in emitted, (
        "no shipped producer emits the frame-truncation reason any more — p33's width refusal is gone"
    )


def test_the_declared_reason_set_is_not_empty_or_degenerate():
    """Anti-vacuity for the generator's ``declared`` arm.

    ``_declared_reason_values`` raises on an empty scan, but a scan that found
    one constant would pass it and make the arm a constant. This is that guard.
    """
    assert len(DECLARED_REASONS) >= 5, (
        f"the AST scan found {len(DECLARED_REASONS)} reason value(s) ({sorted(DECLARED_REASONS)}); six ship "
        "today (three proxy, three harness), so the scan is matching a fraction of them."
    )
    for value in DECLARED_REASONS:
        assert isinstance(value, str) and value and " " not in value, (
            f"scanned {value!r} as a reason value, which is not the shape a not_verified_reason has — the "
            "scan is picking up a constant that is not a reason."
        )


# ---------------------------------------------------------------------------
# Property 9
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=_cases())
def test_a_harness_non_verification_never_carries_a_passing_state(case):
    """Property 9 — ``verified`` false, ``passed`` false, no third state.

    Quantified over the reason, so the arm the harness branch picks for its copy
    cannot change the STATE it reports. R8.2 is the two booleans; R12.3 is the
    key set — an explicit ``verified: False`` and nothing else new.

    Validates: Requirements 8.1, 8.2, 12.3
    """
    results, reason = case["results"], case["reason"]
    event(f"reason arm: {'absent' if reason is None else ('declared' if reason in DECLARED_REASONS else 'arbitrary')}")
    event(f"shape: {'width' if _mod.HARNESS_FIELD_COUNT_KEY in results else 'capacity'}")

    status, checks = _mod.aggregate_check_results(case["parsed"], results)
    check = _harness_check(checks)

    # R8.2 — both booleans, by identity. `is False` rather than `not check[...]`
    # on purpose: a `0`, a `""` or a `None` would satisfy falsiness while making
    # `is_not_verified_check` (an `is False` test) answer False, so the entry
    # would land in the verified partition and FAIL the run.
    assert check["passed"] is False, (
        f"a harness non-verification for reason {reason!r} reports passed={check['passed']!r} — nothing was "
        "executed, so there is nothing that could have passed (R8.2)"
    )
    assert check["verified"] is False, (
        f"a harness non-verification for reason {reason!r} reports verified={check['verified']!r}; the "
        "discriminator is an `is False` test (lambda_function.py:223), so anything else — including a "
        "falsy value — puts this entry in the verified partition (R8.2/R12.3)"
    )
    assert _mod.is_not_verified_check(check) is True, (
        "the production discriminator does not recognise this entry, so R8.3 cannot hold for it"
    )

    # R8.1 — the reason travels to the check, and the absent case takes the
    # producer's documented default rather than rendering None.
    if reason is None:
        assert check["not_verified_reason"] == NOT_VERIFIED_REASON_FRAME_TRUNCATED, (
            "a harness result carrying no reason no longer defaults to the width reason "
            f"(lambda_function.py:631); it reported {check['not_verified_reason']!r}"
        )
    else:
        assert check["not_verified_reason"] == reason, (
            f"the producer said {reason!r} but the check reports {check['not_verified_reason']!r} — the "
            "reason did not survive the harness branch (R8.1)"
        )

    # R12.3 — no third check state. The key set is compared against a check the
    # SHIPPED width builder produced, so this is "the same three carriers of
    # state as the reason that was here first", not a transcribed list.
    # `LGR-b2f79ef-2` refused a third check field once already.
    assert frozenset(check) == _REFERENCE_CHECK_KEYS, (
        f"the harness check for reason {reason!r} carries keys {sorted(check)}, but a harness check carries "
        f"{sorted(_REFERENCE_CHECK_KEYS)}. A new reason must add no FIELD and no STATE — see "
        "lambda_function.py:508-527, where LGR-b2f79ef-2 is recorded as having refused a third check field."
    )
    # And nowhere in the list is `verified` anything but False: a third state
    # would have to surface as some other value of that key. No producer emits
    # `verified: True` today (checked across lambda/), so this is a real
    # statement rather than a tautology.
    for other in checks:
        if "verified" in other:
            assert other["verified"] is False, (
                f"check {other.get('name')!r} carries verified={other['verified']!r} — a third check state "
                "has appeared, which R12.3 forbids"
            )

    # "Never carries a passing state" at the prose seam too: the run is reported
    # as one in which nothing executed. Keyed on the check NAME
    # (EXECUTION_REPLACING_CHECK_NAMES), which is a different discriminator from
    # `verified`, so this is a second surface rather than a restatement.
    assert _mod.execution_was_verified(checks) is False, (
        "a run whose transform never reached an executor is reported as having been verified (R8.2)"
    )
    assert not [c for c in checks if c.get("name") == "code_interpreter_execution"], (
        "the harness entry joined an execution check instead of replacing it, so the report claims an "
        "execution result for a run that never executed"
    )
    # The status itself is R8.3's subject and is asserted there; recorded as an
    # event so the distribution of statuses this property saw is observable.
    event(f"derived test_status: {status}")


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=_cases())
def test_a_harness_non_verification_neither_passes_nor_fails_the_run(case):
    """Property 9 / R8.3 — excluded from the Verified_Check_Partition.

    Two comparisons, because "excluded" has two observable consequences:

    * the entry is absent from the partition, and the status the run derives is
      the one its REVIEWER checks alone imply; and
    * aggregating the same reviewer block with a run that *did* execute
      successfully derives the same ``test_status`` — which is the value the
      only Choice state over this step reads
      (``$.test_code_result.test_result.status``), so the gate cannot see the
      difference either (``LGR-69ca78b-1``, **invariant**: the governance
      Choices fail closed, so an unread difference is inert).

    Validates: Requirements 8.3, 12.3
    """
    parsed, results, reason = case["parsed"], case["results"], case["reason"]

    harness_status, harness_checks = _mod.aggregate_check_results(parsed, results)
    executed_status, executed_checks = _mod.aggregate_check_results(parsed, _executed_successfully())

    harness_verified = _verified(harness_checks)

    # ANTI-VACUITY, and this is the trap task 3.6 actually hit: a reviewer block
    # that produced no checks emptied the partition and made both statuses
    # "failed" — the equality below would have held for the wrong reason.
    # `LGR-b2f79ef-2`'s caveat is the same point: a `verified: False` entry must
    # never be the only check.
    assert harness_verified, (
        "the reviewer block produced no verified checks, so both sides of this comparison are 'failed' by "
        "the `verified_checks and` guard (lambda_function.py:710) and the equality holds vacuously — the "
        "partition is empty rather than the harness entry being excluded from it."
    )
    assert _verified(executed_checks), "guard: the executed baseline has an empty partition too."

    # The entry is not in the partition, and the partition is exactly the
    # reviewer's checks — nothing execution-derived reached it.
    assert _harness_check(harness_checks) not in harness_verified, (
        f"the harness non-verification for reason {reason!r} is IN the verified partition, so it is "
        "evidence in the all() at lambda_function.py:710 (R8.3)"
    )
    assert len(harness_verified) == len(harness_checks) - 1, (
        f"the harness run's partition holds {len(harness_verified)} of {len(harness_checks)} checks; exactly "
        "one entry (the harness non-verification) must be excluded"
    )
    assert all(c.get("name") != _mod.NOT_VERIFIED_BY_HARNESS_CHECK_NAME for c in harness_verified), (
        "a check named not_verified_by_harness survived the partition (R8.3)"
    )

    # The status is what the reviewer checks alone imply: the harness entry
    # contributed neither a pass nor a failure.
    assert harness_status == _status_of(harness_verified), (
        f"test_status {harness_status!r} disagrees with the status its own verified partition implies "
        f"({_status_of(harness_verified)!r}) — the harness entry is participating in the derivation (R8.3)"
    )

    # And it is the same status a successful execution derives, which is the
    # comparison the gate can see. The executed baseline adds a PASSING
    # execution check, so an equal status means the harness entry neither
    # failed the run nor was silently counted as a pass.
    assert harness_status == executed_status, (
        f"a harness non-verification for reason {reason!r} derived test_status {harness_status!r} while the "
        f"same reviewer block with a successful execution derived {executed_status!r} — the entry moved the "
        "verdict, which R8.3 forbids"
    )
    event(f"status held at {harness_status!r}")
    if harness_status == "passed":
        # The interesting half: a status that WOULD have flipped had the entry
        # reached the all(). Recorded so `--hypothesis-show-statistics` shows how
        # often the property was non-trivial rather than asserting it was.
        event("all-passing reviewer block — the entry COULD have flipped this status")


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=_cases())
def test_the_explicit_verified_false_is_the_only_thing_making_it_advisory(case):
    """Property 9's contrapositive — R12.3's "no third state", the other way.

    The same emitted check with ``verified`` removed becomes an ordinary failing
    check and derives ``"failed"``. Without this, the property above could be
    passing because the entry is inert for some *other* reason — a name the
    partition special-cases, say — rather than because ``verified: False`` is the
    whole discriminator. R12.3 is precisely that the discriminator is that key
    and that no second mechanism exists.

    Validates: Requirements 12.3
    """
    _status, checks = _mod.aggregate_check_results(case["parsed"], case["results"])
    check = _harness_check(checks)

    stripped = {k: v for k, v in check.items() if k != "verified"}
    assert _mod.is_not_verified_check(stripped) is False, (
        "the discriminator still recognises the entry with `verified` removed, so it is reading something "
        "other than that key — R12.3's single explicit discriminator has become two"
    )

    as_verified = [c for c in checks if c is not check] + [stripped]
    assert _status_of(_verified(as_verified)) == "failed", (
        "a failing check with no `verified` key did not derive 'failed', so something other than "
        "`verified: False` is making the harness entry advisory (R12.3)"
    )


# ---------------------------------------------------------------------------
# The deterministic anchor — every reason that SHIPS, certainly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", sorted(_emitted_harness_reasons()))
def test_every_shipped_harness_reason_carries_the_non_passing_state(reason):
    """The three shipped reasons, pinned deterministically rather than sampled.

    The property above draws its ``declared`` arm about a third of the time and
    then picks one of six values, so any single shipped reason appears in roughly
    5% of examples. That is real coverage but thin, and "the property covers the
    reason that ships" should not rest on it. Parametrised over the
    BEHAVIOURALLY discovered set, so a fourth reason emitted by a producer
    becomes a fourth case here with no edit.

    Validates: Requirements 8.1, 8.2, 8.3, 12.3
    """
    parsed = _mod.verdict_to_parsed_results(
        _mod.parse_review_verdict(
            json.dumps({"verdict": "pass", "issues": [], "categories": {"Correctness": "pass", "Security": "pass"}})
        )
    )
    # Both shapes, because a reason is not tied to one: the width builder takes
    # the reason as a parameter and capacity's omits the width keys.
    for shape, results in (("width", _width_shaped(reason, 600, 512)), ("capacity", _capacity_shaped(reason))):
        status, checks = _mod.aggregate_check_results(parsed, results)
        check = _harness_check(checks)

        assert check["passed"] is False, f"{reason} / {shape}: passed={check['passed']!r} (R8.2)"
        assert check["verified"] is False, f"{reason} / {shape}: verified={check['verified']!r} (R8.2)"
        assert check["not_verified_reason"] == reason, (
            f"{reason} / {shape}: the check reports {check['not_verified_reason']!r} (R8.1)"
        )
        assert frozenset(check) == _REFERENCE_CHECK_KEYS, (
            f"{reason} / {shape}: keys {sorted(check)} against {sorted(_REFERENCE_CHECK_KEYS)} (R12.3)"
        )
        assert _mod.is_not_verified_check(check) is True, (
            f"{reason} / {shape}: the production discriminator does not recognise the entry (R8.3)"
        )
        assert check not in _verified(checks), f"{reason} / {shape}: the entry is in the verified partition (R8.3)"
        # Non-vacuity: an all-pass reviewer block with two categories yields
        # three verified checks, so "passed" here is a status the entry WOULD
        # have flipped had it reached the all().
        assert len(_verified(checks)) == 3, (
            f"{reason} / {shape}: expected three reviewer checks in the partition, got "
            f"{[c['name'] for c in checks]} — the comparison below would not be over a status that could move"
        )
        assert status == "passed", (
            f"an all-passing reviewer block plus a {reason!r} non-verification ({shape} shape) derived "
            f"{status!r} — the entry failed the run it neither passed nor failed (R8.3)"
        )
        assert _mod.execution_was_verified(checks) is False, (
            f"{reason} / {shape}: a run that never reached an executor is reported as verified (R8.2)"
        )
