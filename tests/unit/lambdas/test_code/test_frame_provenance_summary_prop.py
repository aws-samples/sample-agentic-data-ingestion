# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Feature: p44-verify-against-real-source-data, Property 8: The provenance rides
# in `summary` and `output_data` keeps five keys
"""Property 8 — the frame provenance rides inside ``summary``, never as a sixth key.

Spec ``p44-verify-against-real-source-data`` task 14.2, design §4.7, R5.6.

    *For any* frame outcome, ``create_output_data``'s result has exactly the five
    fixed ``output_data`` keys, and the frame provenance is reachable inside
    ``summary``; a run with no frame provenance carries no empty key.

**Validates: Requirements 5.6**

WHY THIS FILE IS OPTIONAL, AND WHAT IS GENUINELY NEW IN IT
----------------------------------------------------------
The five-key exactness is already enforced twice over, and ``tasks.md`` says so:
``create_output_data`` raises on a sixth (``LGR-60c4985-4``, **template /
ratified**, revisit condition NOT met — "``output_data``'s fixed key set is
widened to carry the SFN return contract"), and
``tests/contracts/api_responses/test_output_data_contract.py`` pins the shape from
the other side. What is new here is **one key's presence inside a free-form
dict**: ``summary`` is validated only as ``isinstance(…, dict)``
(``artifact_schema.py:68-70``), so nothing else in the tree would notice
``frame_provenance`` being dropped, renamed, or made unconditional.

HOW THE PRODUCER IS TESTED WITHOUT INVOKING THE HANDLER
------------------------------------------------------
The ``create_output_data`` call this property is about sits ~500 lines into
``lambda_handler``, after the code read, the verification run, the report build
and three DynamoDB writes. Reaching it through the handler would mean mocking all
of that, and the mocks would then be what the property measures.

So the **call expression itself is extracted from the producer's AST and
evaluated** over drawn bindings, with the *real* ``create_output_data`` bound in
its namespace. The thing under test is therefore the shipped call site — not a
transcription of it — and the two facts that make that non-vacuous are asserted
first, before any property runs:

* :func:`test_the_create_output_data_call_site_is_found_exactly_once` — the scan
  keys on ``artifact_type="test_results"``, and there is exactly one such call in
  the module (line **3849** on disk today, recorded as an observation and not
  asserted, because pinning it would fail on any unrelated insertion above it);
* :func:`test_the_call_site_passes_exactly_the_five_fixed_keys` — five keyword
  arguments, no positional, no ``**`` unpack, and the keyword names equal
  ``artifact_schema.REQUIRED_KEYS``. That is ``LGR-60c4985-4``'s fence read at
  the call site: a sixth ``output_data`` key here is a ``TypeError``, not a
  validation failure;
* :func:`test_the_frame_provenance_key_is_a_conditional_unpack_inside_summary` —
  the key is inside the ``summary`` dict and arrives through a conditional
  ``**{…} if … else {}`` unpack, which is the mechanism design §4.7 specifies and
  the shape ``verification_note`` already uses at the same site.

``LGR-28b4d75-5`` (**template / ratified**) is the posture for all three: an
expectation read off the thing under test cannot detect it drifting, so the
*expectation* here is the requirement (five keys, the provenance reachable) while
the *producer* is what gets evaluated.

THE FIVE KEYS ARE A PROPERTY OF THE RETURN, NOT OF THE VARIABLE
--------------------------------------------------------------
Worth stating because a reader checking this by eye will see six keys.
Immediately after the call, the handler does::

    test_output_data["approval"] = convert_floats_to_decimal(test_approval_payload)

(``lambda_function.py:3888``, read). So ``test_output_data`` carries **six** keys
for the rest of the invocation — ``approval`` is added *after* validation, by
assignment, which ``create_output_data`` neither sees nor forbids. Property 8's
"exactly five" is a claim about what ``create_output_data`` **returns**, which is
what this file evaluates and asserts. Nothing here asserts anything about
``approval``; the Approval_Payload's own contract is
``tests/contracts/api_responses/test_test_failure_approval_payload.py``.

THE PROVENANCE DICTS ARE BUILT BY THE REAL PRODUCER
---------------------------------------------------
Every provenance value drawn below comes from ``FrameSource.provenance()``
(``glue_session_runner.py:415-484``), never transcribed. Its six keys are pinned
as an equality by task 5.2's ``test_frame_provenance.py:224``, so that is
deliberately **not** re-asserted here — if a key is renamed there, that file fails
with the key named, and this one keeps passing because it compares the dict to
itself. What this file adds is that whatever that dict is, it arrives in
``summary`` unchanged.

NON-VACUITY, MEASURED RATHER THAN ARGUED
----------------------------------------
A property that evaluates the producer can pass for the wrong reason, so the three
mutations that matter were applied to the call's AST **in memory** — the producer
file was never edited — and each was confirmed to fail, by the conjunct a reader
would predict:

===========================================  ==========================================
mutation                                     what rejects it
===========================================  ==========================================
delete the conditional unpack                conjunct 3, on a truthy arm; and the
                                             mechanism guard
make it unconditional                        conjunct 4, on **both** falsy arms
                                             (``None`` and ``{}``); and the
                                             mechanism guard
move it to a sixth ``output_data`` keyword   ``TypeError`` from
                                             ``create_output_data``; and
                                             :func:`test_the_call_site_passes_exactly_the_five_fixed_keys`
===========================================  ==========================================

One result is worth stating because it looks like a hole and is not: with the key
**deleted**, the property still passes on the ``None`` arm. It must — a deleted key
and an absent provenance are the same observation there. Conjunct 3, on an arm that
*has* a frame, is the only thing that separates them, which is why the arm
enumeration and :func:`test_the_property_reaches_a_provenance_carrying_arm_and_a_falsy_one`
are load-bearing rather than decorative.

WHAT THIS FILE DOES NOT DO
--------------------------
* **No session, no AWS call, no boto3 client** (do-NOT 1). ``FrameSource`` and
  ``create_output_data`` are both pure, and ``lambda_function.py`` is *parsed*
  rather than imported — so this file needs none of the module-stub preamble its
  siblings in this directory carry.
* **It does not import ``artifact_schema`` by name.** Nine test files in this
  directory put a ``MagicMock`` under that name in ``sys.modules`` to import the
  handler; a plain ``import artifact_schema`` here would collect whichever of
  them ran first, and every assertion below would pass against a mock. The module
  is loaded from its file path instead (:func:`_load_module_from_path`), which is
  safe because it has no imports of its own.
* **It asserts nothing about DynamoDB serialisation.** That the provenance is a
  plain dict the converter walks into is task 5.2's
  ``test_the_provenance_is_a_plain_dict_that_survives_the_dynamodb_converter``.
* **It asserts nothing about the report.** Which wording each frame gets is
  Property 6, and where it sits in the report is Property 7.

RECORDS THAT GOVERN THE CODE UNDER TEST
---------------------------------------
* ``LGR-60c4985-4`` (**template / ratified**, read in full) — ``output_data`` is
  fixed to five validated keys, and its body states them by name. Its scope is
  the resume cache, so it is quoted here for the five-key claim its reasoning
  rests on rather than as a fence on ``artifact_schema.py`` itself. Revisit
  condition unmet.
* ``LGR-ad78946-3`` (**template**, BROAD/60 files) — the shadow comparison went
  to its own state row *because* ``output_data`` "cannot carry a comparison
  payload". Same fence, applied to a different payload; cited as the precedent
  that a new fact goes somewhere other than a sixth key.
* ``LGR-42dbdb1-1`` (**template / ratified**) — the error path deliberately writes
  no ``output_data``, rejecting "calling create_output_data with values the error
  path does not have, and LGR-60c4985-4 fixes it to five validated keys". It is
  why this property has nothing to say about the ``except`` path: there is no call
  there to have a summary.
* ``LGR-28b4d75-5`` (**template / ratified**) — why the call site is extracted and
  evaluated rather than transcribed, and why each derivation carries an
  anti-vacuity guard.
* ``why`` on ``shared/artifact_schema.py`` returns **three commits and zero
  records** — two publication-cleanup snapshots and a p12a lint sweep, none of
  which names the file's contents. So there is no fence specific to that module;
  the five-key rule lives entirely in ``LGR-60c4985-4``'s body. Recorded so the
  next reader does not spend the lookup again.
"""

import ast
import copy
import importlib.util
import os
import pathlib
import sys
import types

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

# Region/account before anything imports boto3 transitively. LGR-3e45e65-1 pins
# the region in tests/unit/conftest.py, so these setdefaults are consistent
# no-ops.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
_TEST_CODE_DIR = _REPO_ROOT / "lambda" / "agentic_data_ingestion_test_code"
_SHARED_DIR = _REPO_ROOT / "shared"
_CONTRACTS_DIR = _REPO_ROOT / "tests" / "contracts" / "api_responses"

_LAMBDA_SOURCE = _TEST_CODE_DIR / "lambda_function.py"
_ARTIFACT_SCHEMA_SOURCE = _SHARED_DIR / "artifact_schema.py"
_CONTRACT_SOURCE = _CONTRACTS_DIR / "test_output_data_contract.py"

# `glue_session_runner` imports `code_interpreter_runner` from its own directory,
# so the path entry is needed even though the module is loaded by name below.
# Nothing in this directory stubs `glue_session_runner` (verified by grep for
# `"glue_session_runner": MagicMock` across tests/ — no hits), so a plain import
# is safe for it in a way it is not for `artifact_schema`.
if str(_TEST_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_TEST_CODE_DIR))

from glue_session_runner import (  # imported after the sys.path entry above
    FRAME_MODE_SAMPLE,
    FRAME_MODE_SYNTHETIC,
    SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
    SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT,
    FrameSource,
    sample_read_format,
)


def _load_module_from_path(name: str, path: pathlib.Path) -> types.ModuleType:
    """Load a module from its file, bypassing ``sys.modules`` entirely.

    Nine files in this directory register ``sys.modules["artifact_schema"] =
    MagicMock()`` so they can import the handler without the shared layer. Within
    one pytest process this file may run after any of them, and a plain import
    would then bind ``create_output_data`` to a mock that accepts a sixth keyword,
    returns a ``MagicMock``, and makes every assertion below pass vacuously.

    Loading from the path under a private name is immune to that, and it is safe
    for this particular module because it imports nothing at all
    (``artifact_schema.py`` is constants plus two functions).
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"could not load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_artifact_schema = _load_module_from_path("_p44_real_artifact_schema", _ARTIFACT_SCHEMA_SOURCE)

create_output_data = _artifact_schema.create_output_data
validate_output_data = _artifact_schema.validate_output_data

#: The five fixed keys, read from the producer rather than written out here, so
#: widening the schema tightens this property instead of silently passing it.
REQUIRED_KEYS = _artifact_schema.REQUIRED_KEYS


def test_the_real_artifact_schema_module_was_loaded_not_a_stub():
    """Anti-vacuity for the whole file: everything below rests on this.

    A ``MagicMock`` in place of ``create_output_data`` would satisfy every
    assertion in this file — it accepts any keywords and returns an object whose
    every attribute access succeeds. This test is the one that fails, naming the
    cause, if the path load is ever replaced by a plain import.
    """
    assert isinstance(REQUIRED_KEYS, frozenset), f"REQUIRED_KEYS is {type(REQUIRED_KEYS).__name__}, not a frozenset"
    assert len(REQUIRED_KEYS) == 5, f"the fixed key set is no longer five keys: {sorted(REQUIRED_KEYS)}"
    assert callable(create_output_data)
    # The real function refuses an unknown artifact_type; a mock would not.
    with pytest.raises(ValueError):
        create_output_data(
            artifact_type="not_an_artifact_type",
            primary_id="p",
            status="success",
            summary={},
            s3_uris={},
        )


# ══════════════════════════════════════════════════════════════════════
# The call site, extracted from the producer (LGR-28b4d75-5)
# ══════════════════════════════════════════════════════════════════════

_LAMBDA_TREE = ast.parse(_LAMBDA_SOURCE.read_text(encoding="utf-8"))


def _test_results_call_sites() -> list[ast.Call]:
    """Every ``create_output_data(artifact_type="test_results", …)`` call.

    Keyed on the **literal ``artifact_type``** rather than on the callee name
    alone, so that a second ``create_output_data`` call added to this module for a
    different artifact — the error path deliberately has none today
    (``LGR-42dbdb1-1``) — does not silently become the site this property
    measures.
    """
    sites: list[ast.Call] = []
    for node in ast.walk(_LAMBDA_TREE):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "create_output_data":
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "artifact_type"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "test_results"
            ):
                sites.append(node)
                break
    return sites


_CALL_SITES = _test_results_call_sites()

#: The one call this property is about. Line 3849 on disk today — an observation,
#: not an assertion.
_CALL = _CALL_SITES[0] if _CALL_SITES else None


def test_the_create_output_data_call_site_is_found_exactly_once():
    """The premise of every property below: there is one such call, and it is found.

    With no site found, :data:`_CALL` is ``None`` and the properties skip, which
    would leave R5.6 unasserted while the suite stayed green. This test is what
    fails loudly instead, and it names the right thing: the handler stopped
    building its ``test_results`` ``output_data`` through
    ``create_output_data``, so R5.6's carrier is gone and the requirement needs
    re-reading rather than the property re-tuning.
    """
    assert len(_CALL_SITES) == 1, (
        f"expected exactly one create_output_data(artifact_type='test_results', …) call, "
        f"found {[node.lineno for node in _CALL_SITES]}"
    )


def test_the_call_site_passes_exactly_the_five_fixed_keys():
    """``LGR-60c4985-4``'s fence, read at the call site rather than at the validator.

    ``create_output_data`` takes five parameters, so a sixth ``output_data`` key
    is a ``TypeError`` here — it never reaches ``validate_output_data``'s
    ``REQUIRED_KEYS`` check. That is why the fence is asserted against the call's
    keywords: they are what would have to grow for the schema to widen, and the
    record's revisit condition ("``output_data``'s fixed key set is widened to
    carry the SFN return contract") is the thing that would license it.

    ``**`` unpacks are rejected for the same reason: ``**extra`` at this level
    would make the key set unreadable from the source and would defeat the
    guard.
    """
    assert _CALL is not None
    assert not _CALL.args, f"the call gained {len(_CALL.args)} positional argument(s); the guard reads keywords"
    passed = [keyword.arg for keyword in _CALL.keywords]
    assert None not in passed, "the call gained a ** unpack — the output_data key set is no longer readable here"
    assert set(passed) == set(REQUIRED_KEYS), (
        f"the call site passes {sorted(passed)} but artifact_schema fixes the set to "
        f"{sorted(REQUIRED_KEYS)} (LGR-60c4985-4). A sixth key belongs inside `summary`."
    )


def _summary_dict_node() -> ast.Dict:
    assert _CALL is not None
    for keyword in _CALL.keywords:
        if keyword.arg == "summary":
            assert isinstance(keyword.value, ast.Dict), (
                f"`summary` is a {type(keyword.value).__name__}, not a dict literal — this file's "
                f"mechanism guard reads the literal's keys"
            )
            return keyword.value
    raise AssertionError("the call site has no `summary` keyword")


def _conditionally_unpacked_keys(node: ast.Dict) -> set[str]:
    """The keys arriving through a ``**({"k": v} if cond else {})`` unpack.

    A ``**`` unpack appears in ``ast.Dict`` as a ``None`` key paired with the
    unpacked value, so the ``None`` keys are exactly the unpack sites.
    """
    keys: set[str] = set()
    for key, value in zip(node.keys, node.values, strict=True):
        if key is not None or not isinstance(value, ast.IfExp):
            continue
        for branch in (value.body, value.orelse):
            if isinstance(branch, ast.Dict):
                keys.update(
                    entry.value
                    for entry in branch.keys
                    if isinstance(entry, ast.Constant) and isinstance(entry.value, str)
                )
    return keys


def test_the_frame_provenance_key_is_a_conditional_unpack_inside_summary():
    """The mechanism guard: design §4.7's shape, not merely the key's presence.

    Three claims, and each one is a way the property below could go quiet:

    1. ``frame_provenance`` is a key of the ``summary`` literal, so it is inside
       the sanctioned carrier and not a sixth ``output_data`` key (C8, R5.6);
    2. it arrives through a **conditional** unpack, which is what makes "a run
       with no frame provenance carries no empty key" a fact about the producer
       rather than about the values this file happens to draw. An unconditional
       ``"frame_provenance": frame_provenance`` would put ``None`` in the summary
       on every ``pandas`` run and still satisfy a property that only ever drew
       truthy dicts;
    3. it sits beside ``verification_note``, whose identical shape at the same
       site is what design §4.7 says to match — so if that one is ever rewritten,
       this key is on the reader's screen when it happens.
    """
    summary = _summary_dict_node()
    conditional = _conditionally_unpacked_keys(summary)
    literal = {key.value for key in summary.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    assert "frame_provenance" not in literal, (
        "`frame_provenance` is an UNCONDITIONAL summary key. Design §4.7 requires the conditional "
        "shape so a pandas or shadow run — which has no frame — carries no empty key (R5.6)."
    )
    assert "frame_provenance" in conditional, (
        f"`frame_provenance` is not a conditionally unpacked key of `summary`. Conditional keys "
        f"found: {sorted(conditional)}; plain keys: {sorted(literal)}."
    )
    assert "verification_note" in conditional, (
        "`verification_note` is no longer a conditional summary key. Design §4.7 pins "
        "`frame_provenance` to that key's shape, so the two move together."
    )


# ══════════════════════════════════════════════════════════════════════
# Evaluating the shipped call over drawn bindings
# ══════════════════════════════════════════════════════════════════════

#: Every free name the extracted call reads, ``create_output_data`` aside. Derived
#: from the node, so a new local appearing in the summary fails
#: :func:`test_every_free_name_in_the_call_is_bound` with the name in the message
#: rather than raising ``NameError`` from inside a property.
_FREE_NAMES = (
    frozenset(node.id for node in ast.walk(_CALL) if isinstance(node, ast.Name) and node.id != "create_output_data")
    if _CALL is not None
    else frozenset()
)

_CALL_CODE = (
    compile(ast.fix_missing_locations(ast.Expression(body=copy.deepcopy(_CALL))), str(_LAMBDA_SOURCE), "eval")
    if _CALL is not None
    else None
)

#: The bindings this file supplies. Anything the producer reads that is not here
#: is a change to the call site, which the guard below reports as such.
_BOUND_NAMES = frozenset(
    {
        "test_execution_id",
        "test_status",
        "checks_passed",
        "checks_failed",
        "checks_run",
        "review_result",
        "execution_verified",
        "verification_note",
        "frame_provenance",
        "test_output_s3_uri",
    }
)


def test_every_free_name_in_the_call_is_bound():
    """The call site gained or lost a local, and the property must be re-read.

    Without this, a new free name surfaces as a ``NameError`` inside a hypothesis
    example, reported as a falsifying example with a stack trace instead of as
    "the producer now reads something this file does not supply".
    """
    assert _CALL is not None
    unbound = _FREE_NAMES - _BOUND_NAMES
    assert not unbound, (
        f"the create_output_data call now reads {sorted(unbound)}, which this file does not bind. "
        f"Add them to _BOUND_NAMES and to `evaluate` — and check whether the new value belongs in "
        f"this property's draw."
    )
    unused = _BOUND_NAMES - _FREE_NAMES
    assert not unused, f"this file binds {sorted(unused)}, which the call no longer reads — drop them"


def evaluate(**bindings) -> dict:
    """Evaluate the producer's own ``create_output_data`` call with these bindings.

    The real :func:`create_output_data` is bound in the namespace, so the five-key
    validation and the ``TypeError`` on a sixth are the shipped ones.
    """
    namespace = {"create_output_data": create_output_data, **bindings}
    # The evaluated expression is the producer's own AST node, compiled from the
    # shipped source at collection time — not a string this file composed.
    return eval(_CALL_CODE, namespace)


# ══════════════════════════════════════════════════════════════════════
# The frame outcomes, built by the real producer
# ══════════════════════════════════════════════════════════════════════

SYNTHETIC = "synthetic"
SAMPLE = "sample"
SAMPLE_UNAVAILABLE = "sample_unavailable"

#: The three frame outcomes ``FrameSource.provenance()`` can report, plus the two
#: falsy values the conditional is about. ``no_provenance`` is the ``pandas`` and
#: ``shadow`` case and the never-reached-the-runner case; ``empty_dict`` is not
#: producer-reachable — ``provenance()`` always returns six keys — and is drawn
#: because it is the only value that distinguishes the shipped ``if
#: frame_provenance`` from an ``if frame_provenance is not None``.
NO_PROVENANCE = "no_provenance"
EMPTY_DICT = "empty_dict"

PROVENANCE_ARMS = (SYNTHETIC, SAMPLE, SAMPLE_UNAVAILABLE, NO_PROVENANCE, EMPTY_DICT)

#: A ``sourceBucket`` object URI of the shape ``file_validation:632`` writes.
SOURCE_URI = "s3://adi-source-files-123456789012-us-west-2/uploads/orders.csv"

#: The URI a JDBC-sourced pipeline carries (``schema_extract:747``), which is what
#: makes ``sample_unavailable`` the ordinary outcome for a whole class of
#: pipelines rather than an edge case.
JDBC_URI = "jdbc://postgres/public/orders"


def provenance_for(arm: str, *, unavailable_reason: str) -> dict | None:
    """The run's frame provenance, or ``None`` / ``{}`` for the two falsy arms.

    Every dict comes from ``FrameSource.provenance()``, so a producer that renames
    a key or changes a frame value is reflected here rather than contradicted.
    """
    if arm == NO_PROVENANCE:
        return None
    if arm == EMPTY_DICT:
        return {}
    if arm == SYNTHETIC:
        source = FrameSource(mode=FRAME_MODE_SYNTHETIC, requested_mode=FRAME_MODE_SYNTHETIC)
    elif arm == SAMPLE_UNAVAILABLE:
        source = FrameSource(
            mode=FRAME_MODE_SYNTHETIC,
            requested_mode=FRAME_MODE_SAMPLE,
            source_uri=JDBC_URI,
            unavailable_reason=unavailable_reason,
        )
    else:
        read_format = sample_read_format(SOURCE_URI)
        assert read_format is not None, f"{SOURCE_URI} must resolve a reader for this fixture to be a sample"
        source = FrameSource(
            mode=FRAME_MODE_SAMPLE,
            requested_mode=FRAME_MODE_SAMPLE,
            source_uri=SOURCE_URI,
            read_format=read_format,
        )
    return source.provenance()


UNAVAILABLE_REASONS = (SAMPLE_UNAVAILABLE_NOT_S3_SOURCE, SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT)


# ══════════════════════════════════════════════════════════════════════
# Property 8
# ══════════════════════════════════════════════════════════════════════

_SETTINGS = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@given(
    arm=st.sampled_from(PROVENANCE_ARMS),
    unavailable_reason=st.sampled_from(UNAVAILABLE_REASONS),
    test_execution_id=st.text(min_size=1, max_size=40),
    test_status=st.sampled_from(["passed", "failed", "not_verified", "unknown", ""]),
    checks_passed=st.integers(min_value=0, max_value=10**6),
    checks_failed=st.integers(min_value=0, max_value=10**6),
    checks_run=st.integers(min_value=0, max_value=10**6),
    review_result=st.text(max_size=600),
    execution_verified=st.booleans(),
    verification_note=st.one_of(st.none(), st.text(max_size=120)),
    test_output_s3_uri=st.text(max_size=120),
)
@_SETTINGS
def test_property_8_the_provenance_rides_in_summary_and_output_data_keeps_five_keys(
    arm,
    unavailable_reason,
    test_execution_id,
    test_status,
    checks_passed,
    checks_failed,
    checks_run,
    review_result,
    execution_verified,
    verification_note,
    test_output_s3_uri,
):
    """*For any* frame outcome, five ``output_data`` keys and the provenance in ``summary``.

    Four conjuncts, and the fourth is the negative half the requirement's wording
    carries in its last clause:

    1. ``create_output_data``'s result has **exactly** the five fixed keys — an
       equality, not a superset, because the point of C8 is that the set is
       closed;
    2. the result passes the shipped ``validate_output_data``, so a summary
       carrying a nested dict is a legal ``output_data`` and not merely one this
       file accepts;
    3. on a run **with** a frame, ``summary["frame_provenance"]`` is the
       provenance dict unchanged — same keys, same values, nothing filtered on
       the way in;
    4. on a run **without** one, the key is **absent** rather than present-and-
       empty. ``None`` is the ``pandas`` / ``shadow`` / never-ran case; ``{}``
       is what makes the conditional's truthiness polarity observable.

    ``frame_provenance`` is additionally asserted never to appear at the top
    level of ``output_data``, which is the failure C8 exists to prevent and is
    cheap to state here alongside the key-count equality.
    """
    provenance = provenance_for(arm, unavailable_reason=unavailable_reason)
    event(f"arm={arm}")

    output_data = evaluate(
        test_execution_id=test_execution_id,
        test_status=test_status,
        checks_passed=checks_passed,
        checks_failed=checks_failed,
        checks_run=checks_run,
        review_result=review_result,
        execution_verified=execution_verified,
        verification_note=verification_note,
        frame_provenance=provenance,
        test_output_s3_uri=test_output_s3_uri,
    )

    # (1) exactly five, and none of them is the provenance
    assert set(output_data) == set(REQUIRED_KEYS), (
        f"output_data keys are {sorted(output_data)}, not the five fixed keys {sorted(REQUIRED_KEYS)} (LGR-60c4985-4)"
    )
    assert "frame_provenance" not in output_data, (
        "`frame_provenance` reached output_data as a top-level key. It belongs inside `summary` "
        "(C8, R5.6) — create_output_data fixes the set to five and a sixth raises."
    )

    # (2) the shipped validator accepts it
    validate_output_data(output_data)

    summary = output_data["summary"]

    if provenance:
        # (3) reachable inside `summary`, unchanged
        assert "frame_provenance" in summary, (
            f"arm={arm}: the run has a frame provenance but `summary` does not carry it. "
            f"summary keys: {sorted(summary)}"
        )
        assert summary["frame_provenance"] == provenance, (
            f"arm={arm}: `summary['frame_provenance']` is {summary['frame_provenance']!r}, "
            f"not the provenance the run produced ({provenance!r})"
        )
    else:
        # (4) no empty key
        assert "frame_provenance" not in summary, (
            f"arm={arm}: frame_provenance is {provenance!r} but `summary` carries the key anyway. "
            f"A pandas or shadow run has no frame and should carry no empty key (R5.6, design §4.7)."
        )


def test_the_property_reaches_a_provenance_carrying_arm_and_a_falsy_one():
    """Non-vacuity for conjuncts 3 and 4, asserted rather than left to the draw.

    ``st.sampled_from`` over five arms will reach all of them long before 200
    examples, but "will" is not "does". This pins that the two branches the
    property's ``if`` selects are both reachable *and* that they differ — a
    ``provenance_for`` that returned ``None`` for every arm would leave conjunct 3
    unexercised and the property still green.
    """
    with_frame = provenance_for(SAMPLE, unavailable_reason=SAMPLE_UNAVAILABLE_NOT_S3_SOURCE)
    without = provenance_for(NO_PROVENANCE, unavailable_reason=SAMPLE_UNAVAILABLE_NOT_S3_SOURCE)
    assert with_frame, "the sample arm must produce a truthy provenance"
    assert not without, "the no-provenance arm must produce a falsy value"
    assert provenance_for(SYNTHETIC, unavailable_reason=SAMPLE_UNAVAILABLE_NOT_S3_SOURCE)
    assert provenance_for(SAMPLE_UNAVAILABLE, unavailable_reason=SAMPLE_UNAVAILABLE_NOT_S3_SOURCE)
    assert provenance_for(EMPTY_DICT, unavailable_reason=SAMPLE_UNAVAILABLE_NOT_S3_SOURCE) == {}


def test_a_sixth_output_data_key_is_a_typeerror_at_this_call_site():
    """Why the new fact went inside ``summary``, demonstrated rather than cited.

    ``LGR-60c4985-4`` fixes ``output_data`` to five keys; the mechanism that
    enforces it at *this* site is Python's own argument binding, not
    ``validate_output_data``. Stating it as an executable fact is what makes the
    "inside ``summary``, never as a sixth key" claim in design §4.7 checkable
    without re-reading the record.
    """
    with pytest.raises(TypeError):
        create_output_data(
            artifact_type="test_results",
            primary_id="tr-101",
            status="success",
            summary={},
            s3_uris={},
            frame_provenance={"frame": "sample"},
        )


# ══════════════════════════════════════════════════════════════════════
# The contract layer's inertness — asserted, not assumed
# ══════════════════════════════════════════════════════════════════════
#
# Design §4.7 and C8 both rest on `test_output_data_contract.py:145` using a `>=`
# shape, "so an added summary key is inert for it". The task asks for that
# inertness to be asserted rather than assumed, and it has two halves: the shape
# of the check, and the consequence of the shape.
#
# The contract module is read as SOURCE, not imported. It does
# `from artifact_schema import ARTIFACT_TYPES, validate_output_data` at module
# level, so importing it from inside this directory's pytest process could bind
# those names to a sibling's MagicMock and make the demonstration vacuous.

_CONTRACT_TREE = ast.parse(_CONTRACT_SOURCE.read_text(encoding="utf-8"))


def _literal_named(tree: ast.Module, name: str, *, within: str | None = None):
    """The value of a module- or class-level assignment to *name*, literal-evaluated."""
    scopes: list[ast.AST] = [tree]
    if within is not None:
        scopes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == within]
        assert scopes, f"no class named {within} in {_CONTRACT_SOURCE.name}"
    for scope in scopes:
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets
            ):
                return ast.literal_eval(node.value)
    raise AssertionError(f"no assignment to {name} found in {_CONTRACT_SOURCE.name}")


def _summary_key_comparison_ops() -> list[type[ast.cmpop]]:
    """The comparison operators used against ``EXPECTED_SUMMARY_KEYS``."""
    ops: list[type[ast.cmpop]] = []
    for node in ast.walk(_CONTRACT_TREE):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for operand in operands:
            if isinstance(operand, ast.Attribute) and operand.attr == "EXPECTED_SUMMARY_KEYS":
                ops.extend(type(op) for op in node.ops)
                break
    return ops


def test_the_contract_layers_summary_check_is_a_superset_check():
    """C8's premise: the contract compares with ``>=``, so extra keys are allowed.

    If this ever becomes ``==``, the ``summary`` carrier stops being free-form
    from the contract layer's point of view, and R5.6's placement decision has to
    be revisited — not this file's assertion tuned. The message says so.
    """
    ops = _summary_key_comparison_ops()
    assert ops, (
        f"no comparison against EXPECTED_SUMMARY_KEYS found in {_CONTRACT_SOURCE.name}. "
        f"C8's inertness claim rests on that check existing."
    )
    assert all(op is ast.GtE for op in ops), (
        f"{_CONTRACT_SOURCE.name} compares summary keys with {[op.__name__ for op in ops]}, not `>=`. "
        f"A `==` there makes an added `summary` key a contract failure, which is the premise "
        f"design §4.7 and C8 rely on being false."
    )


def test_adding_the_provenance_key_is_inert_for_the_contract_layer():
    """The consequence of the ``>=`` shape, demonstrated on the contract's own data.

    Both halves of the claim, over the two samples that matter:

    * the sample the ``>=`` check actually reads (``code_generation``) still
      satisfies it with an extra key present — that is the inertness itself;
    * the ``test_results`` sample, which is the artifact p44 adds the key to,
      still passes the shipped ``validate_output_data`` with a real
      ``FrameSource.provenance()`` dict inside its summary.

    Both fixtures are ``literal_eval``-ed out of the contract file, so a change
    to either sample is reflected here instead of contradicted.
    """
    expected = _literal_named(_CONTRACT_TREE, "EXPECTED_SUMMARY_KEYS", within="TestCreateCodeSummaryKeys")
    create_code_sample = _literal_named(_CONTRACT_TREE, "CREATE_CODE_OUTPUT_DATA")

    baseline = set(create_code_sample["summary"])
    assert baseline >= expected, f"the contract's own sample already fails its check: {sorted(expected - baseline)}"

    provenance = provenance_for(SAMPLE, unavailable_reason=SAMPLE_UNAVAILABLE_NOT_S3_SOURCE)
    widened = {**create_code_sample["summary"], "frame_provenance": provenance}
    assert set(widened) >= expected, (
        f"adding `frame_provenance` broke the contract's superset check — missing {sorted(expected - set(widened))}"
    )

    test_results_sample = _test_results_contract_sample()
    test_results_sample["summary"] = {**test_results_sample["summary"], "frame_provenance": provenance}
    validate_output_data(test_results_sample)


def _test_results_contract_sample() -> dict:
    """The ``test_results`` entry of ``ALL_ARTIFACT_SAMPLES``, literal-evaluated.

    The dict as a whole is not a literal — its ``code_generation`` value is a
    ``Name`` referring to ``CREATE_CODE_OUTPUT_DATA`` — so the one entry is
    extracted rather than the container.
    """
    for node in ast.walk(_CONTRACT_TREE):
        if not (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "ALL_ARTIFACT_SAMPLES" for target in node.targets)
            and isinstance(node.value, ast.Dict)
        ):
            continue
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "test_results":
                return ast.literal_eval(value)
    raise AssertionError("no ALL_ARTIFACT_SAMPLES['test_results'] entry found")
