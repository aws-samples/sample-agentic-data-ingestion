# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 4: no Glue result claims a proxy non-verification.

Spec ``real-spark-verification`` task 8.3; **amended by**
``p44-verify-against-real-source-data`` task 7.1 — see "The tenth outcome"
below.

    *For any* outcome the Glue executor can produce, the returned result carries
    no proxy non-verification marker.

**Validates: Requirement 3.2** — "THE Glue_Session_Runner SHALL report no
Proxy_Non_Verification marker on any result."

**No Glue session is created anywhere in this file.** Every AWS call goes
through a ``MagicMock`` and ``time.sleep`` is patched out. Nothing is deployed
and no production code is modified by this task. p44 task 7.1 added a second,
measured guard behind that claim — :func:`no_glue_api_calls`, which raises a
:class:`BaseException` subclass from ``botocore``'s own call seam for the six
session operations — because the tenth outcome is the file's first draw that
reaches ``CreateSession`` *in order to* observe a failed read of a real object,
so "the client is a mock" is worth measuring rather than asserting in prose
(p44 do-NOT 1: interactive sessions bill).

The tenth outcome (p44 task 7.1)
--------------------------------

**This file's enumeration went from nine to ten, and nothing was deleted or
relaxed.** ``LGR-60c4985-6`` (**template / ratified**) is the discipline for
exactly this edit: amend the assertion, never delete it, and keep the reversal
legible as a reversal rather than let it read as a regression.
``LGR-5906ec6-2`` (**template / ratified**) is where the convention of encoding
a deliberate hold as a test assertion comes from — the flip side of which is
that widening the held behaviour means changing a test **on purpose**.

What arrived: p44 task 6.1 routed a Sample_Frame run whose frame-build stage
failed away from :func:`~glue_session_runner.results_from_outcomes` and onto
:func:`~glue_session_runner.sample_read_failure_results` /
:func:`~glue_session_runner.sample_read_deadline_results`, a **harness**
non-verification carrying ``source_read_failed`` (p44 R2.1, R2.2, R3.4, R4.1).
So ``run_test``'s outcome space is ten and its harness non-verifications are
four, and neither :data:`ALL_KINDS` nor :data:`HARNESS_KINDS` could see the new
route until it was named here.

**The file was green before this amendment, and that was the problem.** Nothing
failed when the tenth outcome landed: this file contains no ``frame_mode``, both
runner constructions took the ``FRAME_MODE_SYNTHETIC`` default, so every draw
was a synthetic run — and task 6.1 left synthetic routing byte-identical on
purpose. :func:`test_the_harness_marker_is_reachable`'s distinct-reason
assertion quantifies over :data:`HARNESS_KINDS`, this file's own table, not over
the module's reason vocabulary, so a tenth reachable outcome existing in the
code could not fail it. A green enumeration that is no longer exhaustive is the
condition the "Why the reachability guard is not optional" section below exists
to prevent, one level up: the guard checks that every kind *named* is reachable,
and cannot check that every kind *reachable* is named. That gap is closed by
reading ``run_test``'s routing, which is what the outcome-space comment block
records and why it is a comment rather than a docstring nobody diffs.

**Not relaxed to a subset.** ``len(set(reasons.values())) == len(HARNESS_KINDS)``
stays an exact equality and :data:`_EXPECTED_GROUP` stays total over
:data:`ALL_KINDS`. A subset form would hold if a reason were later deleted,
which is the thing these exist to notice.

The marker, and why the property is not "no ``not_verified_*`` key"
------------------------------------------------------------------

There are **two** non-verification markers and they attribute the outcome to
different parties:

* ``not_verified_by_proxy`` — the Pandas_Proxy could not express the transform.
  Forbidden here by R3.2, because this executor has no proxy.
* ``not_verified_by_harness`` — the *harness* could not set the run up. Entirely
  legitimate here, and this runner reaches it on **four** paths: the frame
  refusal (two width reasons, ``code_interpreter_runner._frame_refusal``), the
  capacity refusal (``session_capacity_exhausted``), the frame-unbuildable
  refusal (``test_frame_unbuildable``) and — added by p44 task 6.1 — a
  Sample_Frame run whose source read failed (``source_read_failed``).

``not_verified_reason`` is carried by **both**, so a property asserting that no
``not_verified_*`` key appears would forbid four outcomes the requirements
require. What is forbidden is the proxy marker and its proxy-*only* companions,
and that set is **derived** from the declared contract rather than typed out
here — see :data:`PROXY_ONLY_KEYS` and the two guards that check the derivation
is neither vacuous nor over-wide.

What this generalises, and what it does not duplicate
-----------------------------------------------------

* ``test_run_test_composition.py::test_no_outcome_carries_a_proxy_non_verification``
  (task 8.1) is the worked example: four outcomes, parametrized, driven through
  the composed method. Those stay — a named case is easier to read than a
  counterexample. What this file adds is the **quantifier**: every outcome the
  executor can reach, across ten kinds and their variant axes, so an outcome
  nobody thought to write down is covered by construction.
* ``test_run_test_composition.py::test_the_module_imports_no_converter`` asserts
  the *cause* on two named symbols. :func:`test_no_converter_symbol_is_bound_in_the_runner`
  generalises that to **every symbol the converter module defines**, which is
  what a property over outcomes cannot see: an import that is present but not
  yet used produces no marker today and is exactly the edit R3.2 needs to fail
  on.
* **Task 8.2 owns result *shape*** — key-set and per-key type equivalence
  against ``CodeInterpreterTestRunner``. Nothing here asserts a shape. This file
  owns marker *absence* only, and the one shape-adjacent assertion it makes
  (:func:`test_the_harness_marker_is_reachable`) exists to stop the absence
  claim from being vacuous, not to pin a shape.
* Task 3.4's ``test_glue_result_mapping_prop.py`` generalises §4.5's mapping
  table over the builders. This file drives the **composed public method**, so
  the claim is about what a caller can actually receive.

Why the reachability guard is not optional
------------------------------------------

"No result carries key K" is satisfied trivially by a runner that returns
nothing at all, and satisfied almost as cheaply by a generator that only ever
reaches one outcome. Four tests close that:

* :func:`test_the_generator_reaches_every_outcome_kind` — every kind is
  reachable and lands where it claims, at its default variant.
* :func:`test_every_variant_lands_on_the_kind_it_names` — and so does every
  *variant*. This one earned its place: it caught a draft of this file in which
  three non-terminal statement states stalled ``_await_statement``'s poll loop
  and arrived at a different builder, so Property 4 passed on those draws while
  testing nothing it named. See :data:`_BAD_STATEMENT_STATES`.
* :func:`test_both_sample_read_doors_reach_the_source_read_reason` — p44 task
  7.1's addition, and it exists because :func:`_classify` is too coarse to tell
  the tenth kind's two doors apart: both are harness non-verifications, so a
  door that silently stopped reaching its own builder would keep
  :func:`test_every_variant_lands_on_the_kind_it_names` green. Same trap as
  :data:`_BAD_STATEMENT_STATES`, one axis further in.
* :func:`test_the_harness_marker_is_reachable` — the *sibling* marker does
  appear, so the proxy marker's absence is a discrimination and not a silence.

**Falsifiability was checked rather than assumed.** Before this file was
finished, each ``*_results`` builder was wrapped in memory (never edited on
disk) to add one forbidden key, and both properties were confirmed to fail on
exactly the outcome kinds that route through that builder: three kinds for
``not_executed_results``, two for ``statement_failure_results``, one each for
``success_results``, ``session_capacity_results``,
``frame_build_failure_results``, ``lifecycle_failure_results``,
``session_creation_failure_results`` and the imported
``frame_refusal_results`` — nine kinds, all nine caught.

**Re-checked the same way for the tenth** (p44 task 7.1), because a new kind
whose draws never reach the builder they name would make the amendment
cosmetic. ``sample_read_failure_results`` and ``sample_read_deadline_results``
were each wrapped in memory to add one forbidden key, and both properties failed
on :data:`KIND_SAMPLE_READ_FAILURE` and on nothing else — one door each, which
also demonstrates that the two doors reach two different builders rather than one
of them twice. Ten kinds, all ten caught.

Ledger notes
------------

* ``why`` on ``glue_session_runner.py`` and on
  ``tests/fixtures/run_test_result_shapes.py`` returns **no records** — both are
  this spec's own uncommitted files, so the ledger has nothing on them yet.
  Stated as a gap, not as an all-clear. **Re-run for p44 task 7.1 and no longer
  quite true**: ``glue_session_runner.py`` now returns p42's two commits
  (``5c30733eb7``, ``ad78946449``) — ``LGR-5c30733-1/-2/-3`` and the five
  ``ad78946`` records, the latter carrying an explicit ``NOTE: BROAD`` over 60
  files. None governs ``run_test``'s routing or this file's enumeration.
  ``why`` on **this file** returns only the five BROAD ``ad78946`` records, so
  there is still no fence specific to it. ``decisions`` for ``testing``,
  ``error-handling`` and ``api`` (with ``include_backfilled=True``) turn up
  nothing about an outcome enumeration either; the two records that do govern
  this amendment are cited under "The tenth outcome" above and were fetched in
  full.
* ``LGR-4dd9b42-1`` (**template / ratified**) is this spec's authorisation and
  the record R3.2 is about. Its refusal shapes are the ``not_verified_by_proxy``
  payload — ``not_verified_construct``, ``not_verified_line_number``,
  ``not_verified_source_line`` — and its revisit condition is what this whole
  feature answers: "PySpark becomes available to the test environment, at which
  point the refused shapes can be compared against real Spark and converted on
  evidence instead of refused on caution."
* ``LGR-b46a5bb-4`` (template) governs the *form* of the cause assertion.
  Writing this spec's own invariant test, it **rejected substring matching**
  because "it forbids documenting the fence, and failed for exactly that reason
  against ``handlers/agents.py``, whose docstring names ``threading.Thread`` on
  purpose". That trap is live here: ``glue_session_runner``'s own docstrings name
  ``PySpark2PandasConverter`` and ``not_verified_by_proxy`` deliberately, and
  Checkpoint 7 verified those mentions are docstrings only. So the cause is
  asserted on the **module namespace**, never on source text.
* ``LGR-b2f79ef-2`` (template) is why the harness marker's encoding is reused
  rather than extended: the ``passed`` + optional ``verified`` pair already
  expresses three outcomes and a third check *field* was refused once. A fourth
  reason on the existing marker is the shape this runner uses, which is why the
  four harness refusals below are legitimate rather than smells — and why p44's
  ``source_read_failed`` is a fifth *reason* on the same two keys rather than a
  new field or a new state.
* ``LGR-28b4d75-5`` (template) — "a test written from the producer's output
  cannot detect the producer drifting". Every expectation here derives from R3.2
  and from the declared contract in ``tests/fixtures/run_test_result_shapes.py``,
  never from what the runner currently returns.
* ``LGR-3e45e65-1`` (template) — the unit layer pins its region in
  ``tests/unit/conftest.py``, so the ``os.environ`` lines below match the
  sibling files' shape and are not load-bearing.

Codegraph, before this file existed
-----------------------------------

``GlueSessionTestRunner`` 23 callers across 10 covering test files; ``run_test``
1 caller (``test_run_test_composition.py``), covered; ``session_capacity_results``
16 callers, covered by ``test_session_capacity_non_verification.py``.

**Two symbols report no covering tests**: ``results_from_outcomes`` and
``frame_build_failure_results`` — each with one caller, ``run_test``. This file
drives both through the public method, so it *adds* their first coverage. No
characterization test is owed, because this task modifies neither: it is a new
test file and changes no production behaviour. ``glue_session_runner`` imports
nothing from ``shared/``, so the shared-layer blast radius is nil.

Codegraph, re-run for p44 task 7.1
----------------------------------

Over the tenth outcome's route. **Caller counts below are the tool's and are
inflated**, because it attributes this directory's test call sites to the module
under test; the production figure is one caller each, from ``run_test``, read
rather than inferred (``glue_session_runner.py:894-900`` for the deadline door
and ``:964-968`` for the statement door).

* ``sample_read_failure`` — 1 caller, ⚠️ **no covering tests found**. The gate
  for the statement door. p44 task 6.2's ``test_sample_read_failure_prop.py``
  drives it through ``run_test`` rather than calling it, which is why the tool
  still reports none; this file adds a second such route.
* ``sample_read_failure_results`` — 7 callers reported, covered by
  ``test_sample_read_failure_builders.py``.
* ``sample_read_deadline_results`` — 5 callers reported, covered by
  ``test_sample_read_failure_builders.py``.
* ``results_from_outcomes`` — 13 callers reported, covered by
  ``test_results_from_outcomes_characterization.py``. Unchanged by task 6.1 and
  still the destination for every synthetic draw here, which is what makes this
  amendment additive.
* ``run_test`` — the symbol this file drives; task 6.1 changed it, so the two new
  routes are the only behaviour this amendment newly observes.

No production symbol is modified by task 7.1 — it edits this test file only — so
no characterization test is owed. ``glue_session_runner`` still imports nothing
from ``shared/``, so the shared-layer blast radius remains nil.
"""

import os
import sys
import types
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import botocore.client
import hypothesis.strategies as st
import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings
from tests.fixtures.run_test_result_shapes import (
    BASE_CONTRACT_KEYS,
    CONVERSION_REFUSAL,
    FRAME_REFUSAL,
    RUN_TEST_OUTCOME_SHAPES,
)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import glue_session_runner
import pyspark_to_pandas
from glue_session_runner import (
    _MAX_TEST_FRAME_COLUMNS,
    _SESSION_STATES_TERMINAL_BAD,
    _STATEMENT_DEADLINE_SECONDS,
    FRAME_MODE_SAMPLE,
    NOT_VERIFIED_REASON_FRAME_UNBUILDABLE,
    NOT_VERIFIED_REASON_SESSION_CAPACITY,
    NOT_VERIFIED_REASON_SOURCE_READ_FAILED,
    GlueSessionTestRunner,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

#: The transform is irrelevant to this property and is deliberately one the
#: pandas proxy refuses BY NAME (``initcap`` is a ``_UNSUPPORTED_FUNCTIONS``
#: member, ``pyspark_to_pandas.py:573-582``). If any converter were reachable
#: from this runner, this is the input that would make it produce the forbidden
#: marker — so using it makes the property's subject the interesting case rather
#: than an arbitrary one.
TRANSFORM = "df_transformed = df.withColumn('name', initcap(col('name')))"

TARGET_SCHEMA: dict = {"properties": {"name": {"type": "string"}}}

#: A schema the frame builder can type: one column, a known declared type.
BUILDABLE_SCHEMA: dict = {"properties": {"name": {"type": "string"}}}

#: What the frame-build stage prints, in the form ``_parse_results`` reads, so a
#: generated success is a PARSED success rather than an asserted one.
GENERATED_STDOUT = 'GENERATED:{"record_count": 100, "columns": ["name"]}'
VALIDATION_STDOUT = 'VALIDATION:{"record_count": 100, "schema_compliance": 100.0}'

#: The object a :data:`KIND_SAMPLE_READ_FAILURE` run reads, shaped as
#: ``file_validation`` writes it — ``f"s3://{bucket}/{key}"`` from the upload
#: event's own bucket (``file_validation/lambda_function.py:632``), which is
#: ``sourceBucket``. A recognised extension is load-bearing rather than
#: decorative: ``resolve_frame_source`` returns :data:`FRAME_MODE_SAMPLE` only
#: for an ``s3://`` object whose format ``sample_read_format`` resolves, so an
#: unrecognised one would fall back to synthetic and the draw would exercise a
#: different outcome from the one it names. :func:`_drive_sample_read_failure`
#: asserts the resolved frame rather than trusting this constant.
#:
#: One URI, not an axis: p44 task 6.2's ``test_sample_read_failure_prop.py`` owns
#: the URI axis and the "``error_details`` names the object" claim. This file
#: owns marker absence, which no URI can vary.
SAMPLE_SOURCE_URI = "s3://adi-source-files-123456789012-us-west-2/uploads/orders.csv"


# ---------------------------------------------------------------------------
# do-NOT 1, measured rather than asserted in prose (p44 task 7.1)
# ---------------------------------------------------------------------------


class GlueApiGuardViolation(BaseException):
    """A test in this file reached a real Glue API call. Stub the client."""


#: The session operations that bill, plus the polls a run would make against a
#: live session. Enumerated rather than "every Glue operation" so the guard's
#: message can name what was attempted.
GUARDED_GLUE_OPERATIONS = frozenset(
    {"CreateSession", "GetSession", "RunStatement", "GetStatement", "StopSession", "DeleteSession"}
)


@pytest.fixture(autouse=True)
def no_glue_api_calls(monkeypatch):
    """Make a real Glue session call unreachable from this file (p44 do-NOT 1).

    The backstop behind every ``boto3.client`` patch below, for a client the
    construction seam cannot see. Copied from p44 task 6.2's
    ``test_sample_read_failure_prop.py``, which states the argument in full: a
    session bills, and this file's tenth outcome is its first draw that reaches
    ``CreateSession`` deliberately, so ``create_session.assert_not_called()`` is
    not available as the measure.

    ``BaseException`` on purpose: ``run_test``'s third clause is
    ``except Exception`` and would swallow an ``Exception`` raised here, route it
    to ``create_session_failure_results``, and let the test pass while a real API
    call had been attempted — the exact failure mode the guard exists to stop.
    Same mechanism and same reasoning as ``tests/unit/conftest.py``'s
    ``no_cloudwatch_writes``.

    **The passthrough is captured at fixture time, not at import**, which is the
    one deliberate difference from the sibling file: ``tests/unit/conftest.py``
    installs its own ``_make_api_call`` wrapper through an autouse fixture too, so
    a module-level capture of the original would shadow that guard for every
    non-Glue operation depending on fixture order. Reading the attribute here
    composes with whatever is already installed instead.
    """
    passthrough = botocore.client.BaseClient._make_api_call

    def _guarded_make_api_call(self, operation_name, api_params):
        if operation_name in GUARDED_GLUE_OPERATIONS:
            raise GlueApiGuardViolation(
                f"a unit test reached a real Glue {operation_name} call — p44 do-NOT 1 forbids creating "
                f"an interactive session, which bills. Patch `glue_session_runner.boto3.client` before "
                f"constructing the runner rather than weakening this guard."
            )
        return passthrough(self, operation_name, api_params)

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", _guarded_make_api_call)


# ---------------------------------------------------------------------------
# The forbidden key set, derived from the declared contract
# ---------------------------------------------------------------------------
#
# NOT typed out. `CONVERSION_REFUSAL` is the one outcome the fixture flags
# `executor_independent=False`, and its own comment says why: "the one outcome a
# second executor must NOT be able to produce (R3.2)". So the forbidden set is
# *that* outcome's extra keys, minus the ones the harness refusal legitimately
# shares.
#
# Deriving it rather than listing it means a companion key added to the proxy
# refusal later is forbidden here automatically, with no edit — which is the
# failure mode a hand-written list has.

#: Keys the proxy refusal adds on top of the nine-key contract core.
_PROXY_EXTRA_KEYS: frozenset[str] = CONVERSION_REFUSAL.keys - BASE_CONTRACT_KEYS

#: Keys the *harness* refusal adds. Shared vocabulary, not forbidden.
_HARNESS_EXTRA_KEYS: frozenset[str] = FRAME_REFUSAL.keys - BASE_CONTRACT_KEYS

#: What R3.2 forbids on any Glue result: the proxy marker and the companions
#: only it carries. ``not_verified_reason`` drops out of this difference because
#: both refusals carry it, which is exactly the discrimination the property
#: needs and the reason it is computed rather than declared.
PROXY_ONLY_KEYS: frozenset[str] = _PROXY_EXTRA_KEYS - _HARNESS_EXTRA_KEYS


def test_the_forbidden_key_set_is_derived_correctly():
    """Guard on :data:`PROXY_ONLY_KEYS`: non-vacuous, and not over-wide.

    The property below is only as strong as this set. Two ways it could rot
    silently, both closed here:

    * **Vacuous** — an empty or marker-less set makes every assertion below
      pass for free.
    * **Over-wide** — if ``not_verified_reason`` stopped being carried by the
      harness refusal in the fixture, it would fall into this difference and the
      property would start forbidding a key the capacity and frame-unbuildable
      refusals are *required* to carry (R8.1, R7.3), turning a passing suite
      into one that demands a requirement violation.
    """
    assert "not_verified_by_proxy" in PROXY_ONLY_KEYS, "the proxy marker itself must be forbidden, or R3.2 is unchecked"
    expected_proxy_only = {
        "not_verified_by_proxy",
        "not_verified_construct",
        "not_verified_line_number",
        "not_verified_source_line",
    }
    assert expected_proxy_only == PROXY_ONLY_KEYS, (
        "the proxy refusal's companion keys moved; re-read R3.2 before widening this"
    )
    assert "not_verified_reason" in _PROXY_EXTRA_KEYS & _HARNESS_EXTRA_KEYS, (
        "not_verified_reason is SHARED vocabulary — if it stops being shared, this "
        "property starts forbidding a key three harness refusals must carry"
    )
    assert "not_verified_by_harness" not in PROXY_ONLY_KEYS, (
        "the harness marker is legitimate on this runner (R7.3, R8.1)"
    )
    assert CONVERSION_REFUSAL.executor_independent is False, (
        "the fixture stopped flagging the proxy refusal as executor-dependent, which is the declaration R3.2 rests on"
    )


# ---------------------------------------------------------------------------
# The outcome space
# ---------------------------------------------------------------------------
#
# Every outcome `run_test` can reach, enumerated from its own routing rather
# than from a run. Reading `run_test` (`glue_session_runner.py:669-981`) plus
# `results_from_outcomes` (`:3901`) gives TEN. Nine were p42's; the tenth arrived
# with p44 (`p44-verify-against-real-source-data`, R2.1 / R2.2 / R3.4 / R4.1) and
# is marked below:
#
#   client_unavailable   -> not_executed_results            (pre-flight 1)
#   frame_refusal        -> _frame_truncation_results        (pre-flight 3, width)
#   frame_unbuildable    -> frame_build_failure_results      (FrameBuildError)
#   capacity             -> session_capacity_results         (pre-flight 4)
#   create_failure       -> session_creation_failure_results (the else-branch)
#   lifecycle_failure    -> lifecycle_failure_results        (GlueSessionError)
#   success              -> success_results
#   statement_error      -> statement_failure_results        (Output.Status error)
#   statement_bad_state  -> statement_failure_results        (CANCELLED / ERROR)
#   sample_read_failure  -> sample_read_failure_results      (p44: sample frame,
#                        |                                    frame-build stage
#                        |                                    failed, `:964-968`)
#                        -> sample_read_deadline_results     (p44: sample frame,
#                                                             GlueSessionError,
#                                                             `:894-900`)
#
# The tenth is ONE kind with TWO doors, not two kinds, because both reach the same
# `not_verified_reason` — `source_read_failed` — and this file's subject is the
# reason vocabulary rather than the builder count. The doors are a variant axis
# (`Outcome.sample_read_door`), which is what keeps
# `test_every_variant_lands_on_the_kind_it_names` and
# `test_both_sample_read_doors_reach_the_source_read_reason` honest about which
# route a draw took.
#
# Gating for both doors is `frame_source.is_sample`, so all nine kinds above stay
# on their p42 routing byte-identically: every one of them is constructed with
# `frame_mode` at its `FRAME_MODE_SYNTHETIC` default. That is why this amendment
# is additive rather than a rewrite, and why nothing here went red when task 6.1
# landed — see "The tenth outcome" in the module docstring for why a green
# non-exhaustive enumeration is the condition worth fixing.
#
# The p42 task's list named eight; `frame_refusal` and `frame_unbuildable` are two
# distinct harness refusals with different reasons and different builders, so
# they are generated separately.
#
# One destination is deliberately absent and it is absent because it is
# UNREACHABLE, not because nobody named it: `results_from_outcomes`' empty-outcome
# arm (`:3900`) maps to `not_executed_results`, and its own docstring records that
# the empty case cannot be reached through `run_test` because
# `run_guarded_session` always submits the frame build first. It is mapped rather
# than asserted there so a misroute reports something instead of raising
# `IndexError` out of a result mapper. So this table is exhaustive over `run_test`'s
# REACHABLE routing, which is the claim `_EXPECTED_GROUP` and
# `test_the_expected_group_map_is_total_over_every_kind` hold it to.

KIND_SUCCESS = "success"
KIND_STATEMENT_ERROR = "statement_error"
KIND_STATEMENT_BAD_STATE = "statement_bad_state"
KIND_FRAME_REFUSAL = "frame_refusal"
KIND_FRAME_UNBUILDABLE = "frame_unbuildable"
KIND_CAPACITY = "capacity"
KIND_CREATE_FAILURE = "create_failure"
KIND_CLIENT_UNAVAILABLE = "client_unavailable"
KIND_LIFECYCLE_FAILURE = "lifecycle_failure"
#: p44's tenth. A Sample_Frame run whose source read failed.
KIND_SAMPLE_READ_FAILURE = "sample_read_failure"

ALL_KINDS: tuple[str, ...] = (
    KIND_SUCCESS,
    KIND_STATEMENT_ERROR,
    KIND_STATEMENT_BAD_STATE,
    KIND_FRAME_REFUSAL,
    KIND_FRAME_UNBUILDABLE,
    KIND_CAPACITY,
    KIND_CREATE_FAILURE,
    KIND_CLIENT_UNAVAILABLE,
    KIND_LIFECYCLE_FAILURE,
    KIND_SAMPLE_READ_FAILURE,
)

#: The four kinds that are HARNESS non-verifications. Named so the reachability
#: guard can require the sibling marker on exactly these and nowhere else — which
#: is why p44's fourth had to be added here and not only to :data:`ALL_KINDS`: a
#: harness kind left out would fail that guard's ``else`` arm, which asserts the
#: marker is *absent* on every kind this set does not name.
HARNESS_KINDS: frozenset[str] = frozenset(
    {KIND_FRAME_REFUSAL, KIND_FRAME_UNBUILDABLE, KIND_CAPACITY, KIND_SAMPLE_READ_FAILURE}
)

#: The two doors onto ``source_read_failed`` — the variant axis for
#: :data:`KIND_SAMPLE_READ_FAILURE`.
#:
#: ``statement`` is the frame-build statement reaching a terminal state with
#: ``StatementOutcome.failed`` true, gated by
#: ``glue_session_runner.sample_read_failure``. ``deadline`` is the statement never
#: reaching a terminal state inside :data:`_STATEMENT_DEADLINE_SECONDS`, so
#: ``_await_statement`` raises ``GlueSessionError`` and there is no outcome to
#: read — the arm that consults ``last_frame_source`` instead.
#:
#: **The deadline arm is wider than its own wording, and that is recorded
#: behaviour rather than a defect this file found.** ``run_test``'s docstring
#: states it: every ``GlueSessionError`` on a Sample_Frame run takes that route,
#: not only a frame-build deadline, because ``GlueSessionError`` carries no stage
#: and narrowing it would mean sniffing the exception's message. This axis covers
#: the door; it does not object to it.
DOOR_STATEMENT = "statement"
DOOR_DEADLINE = "deadline"
_SAMPLE_READ_DOORS: tuple[str, ...] = (DOOR_STATEMENT, DOOR_DEADLINE)

#: The statement states that are terminal and NOT ``AVAILABLE`` — the two a run
#: driven through the public method can actually end on.
#:
#: **``CANCELLING``, ``TIMEOUT`` and ``""`` are deliberately absent, and the
#: reason is worth stating because an earlier draft of this file had them.**
#: ``_STATEMENT_STATES_TERMINAL`` is ``{AVAILABLE, CANCELLED, ERROR}``
#: (``glue_session_runner.py:160``) and ``_await_statement`` *polls until
#: terminal*, so a mock returning ``CANCELLING`` never leaves the loop: it
#: exhausts the poll responses, and the resulting error is routed by
#: ``run_test``'s broad ``except`` to the creation-failure builder. The draw then
#: silently exercises a different outcome from the one it names — the property
#: still passes, but it stops testing what it claims. Excluding them keeps every
#: draw honest about which route it took, and
#: :func:`test_every_variant_lands_on_the_kind_it_names` is what noticed.
#:
#: Those three states ARE covered, one layer down: task 3.4's
#: ``test_glue_result_mapping_prop.py`` generates them straight into
#: ``statement_failure_results``, where they are legal inputs because the builder
#: is a total function over its argument and does not poll.
_BAD_STATEMENT_STATES: tuple[str, ...] = ("CANCELLED", "ERROR")

#: ``CreateSession`` failures that are NOT capacity. Deliberately excludes
#: ``ResourceNumberLimitExceededException`` — that is :data:`KIND_CAPACITY`, a
#: different builder and a different attribution.
_CREATE_FAILURE_CODES: tuple[str, ...] = (
    "AccessDeniedException",
    "ValidationException",
    "InvalidInputException",
    "ThrottlingException",
    "InternalServiceException",
)

#: Declared field types outside :func:`spark_type_for`'s vocabulary, each of
#: which raises ``FrameBuildError`` host-side. The dict and the list are the
#: JSON-Schema union shapes its docstring names as the non-``str`` case.
_UNBUILDABLE_TYPES: tuple[Any, ...] = ("geography", "interval", "variant", ["string", "null"], {"anyOf": []}, 7, None)

_STAGE_COUNT = 3


# `eq=False` alongside `frozen=True` is deliberate, not a slip. A frozen
# dataclass derives `__hash__` from its fields, and `unbuildable_type` carries the
# two JSON-Schema union shapes `spark_type_for` actually receives — a list and a
# dict — so a field-derived hash raises `TypeError` the moment anything hashes an
# Outcome. Nothing compares Outcomes, so dropping `__eq__` costs nothing and
# leaves identity hashing in its place.
@dataclass(frozen=True, eq=False)
class Outcome:
    """One reachable outcome, described rather than observed.

    ``kind`` selects the route; the remaining fields are the variant axes that
    route has. Every field is inert on the routes that do not read it, which is
    what lets one flat strategy cover all ten.
    """

    kind: str
    #: Which of the three stages misbehaves, for the two statement kinds.
    stage_index: int = 0
    #: The reached state, for :data:`KIND_STATEMENT_BAD_STATE`.
    state: str = "CANCELLED"
    #: Glue's ``ErrorName`` / ``ErrorValue``, for :data:`KIND_STATEMENT_ERROR`.
    #: ``None`` means Glue reported neither, which the runner must survive.
    error_name: str | None = "AnalysisException"
    error_value: str | None = "cannot resolve column"
    #: A traceback with no Python header line, as Glue actually sends it.
    traceback_frames: tuple[str, ...] = ()
    #: The boto3 error code, for :data:`KIND_CREATE_FAILURE`.
    create_error_code: str = "AccessDeniedException"
    #: The declared type that cannot be typed, for :data:`KIND_FRAME_UNBUILDABLE`.
    unbuildable_type: Any = "geography"
    #: How many declared fields the schema carries, for :data:`KIND_FRAME_REFUSAL`.
    #: ``0`` is the no-source-schema arm; above the ceiling is the width arm.
    field_count: int = 0
    #: The session state reached instead of ``READY``, for
    #: :data:`KIND_LIFECYCLE_FAILURE`.
    session_state: str = "FAILED"
    #: Which door onto ``source_read_failed``, for
    #: :data:`KIND_SAMPLE_READ_FAILURE`. The default is :data:`DOOR_STATEMENT`
    #: because that is the door
    #: :func:`test_the_generator_reaches_every_outcome_kind` and
    #: :func:`test_the_harness_marker_is_reachable` drive, both of which use the
    #: default variant, and the statement door is the one that reads Glue's error
    #: fields — so the default draw exercises the wider code path of the two.
    sample_read_door: str = DOOR_STATEMENT


@st.composite
def _outcomes(draw: st.DrawFn) -> Outcome:
    """Generate any outcome ``run_test`` can reach, with its variants."""
    kind = draw(st.sampled_from(ALL_KINDS))
    return Outcome(
        kind=kind,
        stage_index=draw(st.integers(min_value=0, max_value=_STAGE_COUNT - 1)),
        state=draw(st.sampled_from(_BAD_STATEMENT_STATES)),
        error_name=draw(st.one_of(st.none(), st.text(min_size=0, max_size=40))),
        error_value=draw(st.one_of(st.none(), st.text(min_size=0, max_size=60))),
        traceback_frames=draw(
            st.lists(st.text(min_size=0, max_size=80), min_size=0, max_size=4).map(tuple),
        ),
        create_error_code=draw(st.sampled_from(_CREATE_FAILURE_CODES)),
        unbuildable_type=draw(st.sampled_from(_UNBUILDABLE_TYPES)),
        # 0 is the no-schema arm; anything above the ceiling is the width arm.
        # Both are `_frame_refusal`'s and both must precede CreateSession.
        field_count=draw(st.sampled_from((0, _MAX_TEST_FRAME_COLUMNS + 1, _MAX_TEST_FRAME_COLUMNS + 500))),
        session_state=draw(st.sampled_from(tuple(sorted(_SESSION_STATES_TERMINAL_BAD)))),
        sample_read_door=draw(st.sampled_from(_SAMPLE_READ_DOORS)),
    )


# ---------------------------------------------------------------------------
# Driving one outcome through the composed public method
# ---------------------------------------------------------------------------


def _ok_statement(text: str = "") -> dict:
    return {"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": text}}}


def _error_statement(outcome: Outcome) -> dict:
    """An ``AVAILABLE`` statement whose ``Output.Status`` is not ``ok``.

    The ``Traceback`` is a **list of frame strings with no header line**, which
    is what Glue actually sends — the shape R5.5 warns a traceback-sniffing
    classifier would misread as a success. Absent ``ErrorName`` / ``ErrorValue``
    are generated too, because Glue reports neither on some failures.
    """
    output: dict[str, Any] = {"Status": "error", "Data": {"TextPlain": ""}}
    if outcome.error_name is not None:
        output["ErrorName"] = outcome.error_name
    if outcome.error_value is not None:
        output["ErrorValue"] = outcome.error_value
    if outcome.traceback_frames:
        output["Traceback"] = list(outcome.traceback_frames)
    return {"State": "AVAILABLE", "Output": output}


def _source_schema(outcome: Outcome) -> dict:
    """The ``source_schema`` argument that drives *outcome*'s frame behaviour."""
    if outcome.kind == KIND_FRAME_REFUSAL:
        if outcome.field_count == 0:
            # No declared source field at all — `_frame_refusal`'s no-schema arm.
            return {}
        return {"properties": {f"c{i}": {"type": "string"} for i in range(outcome.field_count)}}
    if outcome.kind == KIND_FRAME_UNBUILDABLE:
        # Inside the ceiling and present, so the width arm does NOT fire; what
        # fails is TYPING the field, host-side, in `spark_type_for`.
        return {"properties": {"name": {"type": outcome.unbuildable_type}}}
    return BUILDABLE_SCHEMA


def _client(outcome: Outcome) -> MagicMock:
    """A mock Glue client wired to produce *outcome* and nothing else."""
    client = MagicMock()
    client.create_session.return_value = {"Session": {"Id": "created"}}
    client.get_session.return_value = {"Session": {"Status": "READY"}}
    client.run_statement.side_effect = [{"Id": i + 1} for i in range(_STAGE_COUNT)]

    if outcome.kind == KIND_CAPACITY:
        client.create_session.side_effect = ClientError(
            {"Error": {"Code": "ResourceNumberLimitExceededException", "Message": "limit reached"}},
            "CreateSession",
        )
    elif outcome.kind == KIND_CREATE_FAILURE:
        client.create_session.side_effect = ClientError(
            {"Error": {"Code": outcome.create_error_code, "Message": "refused"}},
            "CreateSession",
        )
    elif outcome.kind == KIND_LIFECYCLE_FAILURE:
        # A state from which READY is unreachable -> GlueSessionError, which
        # `run_test` maps with `lifecycle_failure_results`.
        client.get_session.return_value = {"Session": {"Status": outcome.session_state}}

    # The statement polls. Stage 0 prints the GENERATED marker and stage 2 the
    # VALIDATION marker, so a `success` outcome is one `_parse_results` actually
    # parsed rather than one this helper asserted.
    stdout_by_stage = {0: GENERATED_STDOUT, 1: "", 2: VALIDATION_STDOUT}
    statements: list[dict] = []
    for stage in range(_STAGE_COUNT):
        if outcome.kind == KIND_STATEMENT_ERROR and stage == outcome.stage_index:
            statements.append({"Statement": _error_statement(outcome)})
            break  # `_run_statements` stops at the first failure.
        if outcome.kind == KIND_STATEMENT_BAD_STATE and stage == outcome.stage_index:
            statements.append({"Statement": {"State": outcome.state, "Output": {}}})
            break
        statements.append({"Statement": _ok_statement(stdout_by_stage[stage])})
    client.get_statement.side_effect = statements
    return client


class _StatementPollClock:
    """A monotonic clock that advances only once a statement has been polled.

    The mechanism for :data:`DOOR_DEADLINE`, and it has to be **poll-keyed rather
    than call-counted**. ``_await_session_ready``, ``_await_statement`` and
    ``_release_session`` each read ``time.monotonic`` twice or more, so a clock
    that jumped on the *n*th call would put the jump in a different loop as soon
    as any of those three changed shape — and the wrong loop timing out is a
    different route with a different builder, which is
    :data:`_BAD_STATEMENT_STATES`' trap wearing a different hat.

    Keyed on ``get_statement`` calls instead: zero polls means the session-ready
    loop sees time standing still, so it returns on its first ``READY`` and the
    statement loop enters. The first poll then jumps the clock past
    :data:`_STATEMENT_DEADLINE_SECONDS`, so ``_await_statement``'s
    ``while time.monotonic() < deadline`` exits and it raises — reached through the
    real production loop rather than by patching the raise in.
    ``_release_session`` then recomputes its own deadline from the jumped value
    (``glue_session_runner.py:1382``), so the ``finally``'s stop and delete still
    run and R2.4's release is not quietly skipped.

    Copied from p44 task 6.2's ``test_sample_read_failure_prop.py``, which solved
    the same timing problem first.
    """

    #: Comfortably past every deadline in the module, so one poll is enough.
    _JUMP = 10.0 * _STATEMENT_DEADLINE_SECONDS

    def __init__(self) -> None:
        self.statement_polls = 0

    def __call__(self) -> float:
        return self._JUMP * self.statement_polls


def _sample_client(outcome: Outcome, clock: _StatementPollClock) -> MagicMock:
    """A mock Glue client wired to produce *outcome*'s sample-read door.

    Separate from :func:`_client` rather than another branch inside it, so the
    nine p42 kinds keep the client they have always had: task 6.1 left synthetic
    routing byte-identical, and this amendment is only additive if the draws that
    do not exercise it are too.
    """
    client = MagicMock()
    client.create_session.return_value = {"Session": {"Id": "created"}}
    client.get_session.return_value = {"Session": {"Status": "READY"}}
    # Three ids though only the first is consumed: the frame build fails, so
    # `_run_statements` breaks before the transform is submitted. A shorter list
    # would turn "the transform WAS submitted" into a `StopIteration` instead of
    # the assertion failure it should be.
    client.run_statement.side_effect = [{"Id": index + 1} for index in range(_STAGE_COUNT)]

    if outcome.sample_read_door == DOOR_DEADLINE:

        def _never_terminal(**_kwargs):
            clock.statement_polls += 1
            # `RUNNING` is deliberately not in `_STATEMENT_STATES_TERMINAL`, so
            # the poll loop keeps going and the deadline is what ends it.
            return {"Statement": {"State": "RUNNING"}}

        client.get_statement.side_effect = _never_terminal
        return client

    # The frame build is stage 0, so the first statement IS the source read.
    # `_error_statement` reuses this file's existing `error_name` / `error_value` /
    # `traceback_frames` axes, so the tenth kind inherits their variants for free.
    client.get_statement.side_effect = [{"Statement": _error_statement(outcome)}]
    return client


def _drive_sample_read_failure(outcome: Outcome) -> dict[str, Any]:
    """Drive :data:`KIND_SAMPLE_READ_FAILURE` through the composed public method.

    The one construction in this file that passes ``frame_mode`` — every other
    kind takes the ``FRAME_MODE_SYNTHETIC`` default, which is what keeps their
    routing identical to p42's and this amendment additive.

    Two guards, neither decorative. The client guard is the file's existing one: a
    ``None`` client takes ``run_test``'s first pre-flight branch and returns before
    any frame decision, which would make every property here vacuous. The
    ``last_frame_source`` guard is p44's addition and closes the trap
    :data:`_BAD_STATEMENT_STATES` records — if ``resolve_frame_source`` fell back
    to synthetic for any reason (an extension it does not recognise, a URI shape
    the scheme gate rejects), the draw would exercise ``statement_failure_results``
    while naming the source-read route, and both properties would pass on it.
    """
    clock = _StatementPollClock()
    client = _sample_client(outcome, clock)
    with patch("glue_session_runner.boto3.client", return_value=client):
        runner = GlueSessionTestRunner(
            REGION,
            role_arn=ROLE_ARN,
            frame_mode=FRAME_MODE_SAMPLE,
            source_data_uri=SAMPLE_SOURCE_URI,
        )
    assert runner.client is client, "guard: a None client would make every property below vacuous"

    # `time` is replaced wholesale rather than `monotonic` patched on the real
    # module, so hypothesis' own timing is untouched. `glue_session_runner` reads
    # exactly two attributes off it, `monotonic` and `sleep`.
    fake_time = types.SimpleNamespace(monotonic=clock, sleep=lambda _seconds: None)
    with patch.object(glue_session_runner, "time", fake_time):
        result: dict[str, Any] = runner.run_test(TRANSFORM, _source_schema(outcome), TARGET_SCHEMA)

    resolved = runner.last_frame_source
    assert resolved is not None and resolved.is_sample, (
        f"guard: the run resolved {resolved!r}, not a sample frame, so this draw does not "
        f"exercise the source-read route it names"
    )
    return result


def _drive(outcome: Outcome) -> dict[str, Any]:
    """Run *outcome* through ``GlueSessionTestRunner.run_test`` and return it.

    The **composed public method**, deliberately: R3.2 is a claim about what a
    caller can receive, and a builder called directly cannot show that the
    routing between builders never picks up a marker on the way.

    The result is bound to an annotated local before it is returned, for the
    reason ``glue_session_runner``'s own ``_parse_results`` alias records: the
    checker resolves the sibling module to ``Any``, so returning the call
    directly reports ``no-any-return``.
    """
    result: dict[str, Any]
    if outcome.kind == KIND_SAMPLE_READ_FAILURE:
        # p44's tenth. Its own driver: it is the one kind that needs a frame mode,
        # a source URI and a clock, and folding those into the branches below
        # would put a configuration fact on nine routes that must not read one.
        return _drive_sample_read_failure(outcome)

    if outcome.kind == KIND_CLIENT_UNAVAILABLE:
        with patch("glue_session_runner.boto3.client", side_effect=RuntimeError("no credentials")):
            runner = GlueSessionTestRunner(REGION, role_arn=ROLE_ARN)
        assert runner.client is None, "guard: the client-unavailable route needs a None client"
        result = runner.run_test(TRANSFORM, _source_schema(outcome), TARGET_SCHEMA)
        return result

    client = _client(outcome)
    with patch("glue_session_runner.boto3.client", return_value=client):
        runner = GlueSessionTestRunner(REGION, role_arn=ROLE_ARN)
    assert runner.client is client, "guard: a None client would make every property below vacuous"

    with patch("glue_session_runner.time.sleep"):
        result = runner.run_test(TRANSFORM, _source_schema(outcome), TARGET_SCHEMA)
    return result


# ---------------------------------------------------------------------------
# Property 4 — the consequence
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(outcome=_outcomes())
def test_no_glue_result_carries_a_proxy_non_verification(outcome: Outcome):
    """Property 4: no outcome the Glue executor produces claims a proxy refusal.

    **Validates: Requirement 3.2**

    The whole statement, over every reachable outcome. Asserted as key
    *absence* rather than as a falsy value: ``not_verified_by_proxy: False``
    would still put the key on the wire, where ``aggregate_check_results`` and
    the dashboard's label map both dispatch on presence.
    """
    result = _drive(outcome)

    present = sorted(PROXY_ONLY_KEYS & result.keys())
    assert not present, (
        f"the {outcome.kind!r} outcome carried proxy non-verification key(s) {present} — "
        f"R3.2 forbids any of {sorted(PROXY_ONLY_KEYS)} on a Glue result"
    )


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(outcome=_outcomes())
def test_no_glue_result_carries_a_key_only_the_proxy_refusal_declares(outcome: Outcome):
    """The same absence, stated over the declared contract instead of a key list.

    **Validates: Requirement 3.2**

    Independent of :data:`PROXY_ONLY_KEYS`' arithmetic: every key on the result
    must belong to some outcome the fixture flags ``executor_independent``. A
    key that only the proxy refusal declares is by definition a key this
    executor must not produce, and this catches the case where a *new* proxy-only
    companion is added to the contract and the derived set above is edited to
    accommodate it rather than to forbid it.
    """
    result = _drive(outcome)

    permitted: set[str] = set()
    for shape in RUN_TEST_OUTCOME_SHAPES.values():
        if shape.executor_independent:
            permitted |= set(shape.keys)

    proxy_only = sorted(set(result) - permitted)
    assert not proxy_only, (
        f"the {outcome.kind!r} outcome carried {proxy_only}, which no executor-independent "
        f"outcome declares — the proxy refusal is the only shape that does (R3.2)"
    )


# ---------------------------------------------------------------------------
# The premise, and the non-vacuity guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_the_generator_reaches_every_outcome_kind(kind: str):
    """Every kind the strategy can draw is reachable and lands where it claims.

    Without this the property could pass while eight of nine routes were
    unreachable — a generator that silently stopped firing weakens a property
    invisibly, which is the failure this checks for rather than assumes.

    The classification below is derived from ``run_test``'s routing, not from
    the returned dict's own fields, so a route that started returning some other
    builder's shape fails here.
    """
    result = _drive(Outcome(kind=kind))

    assert result.keys() >= BASE_CONTRACT_KEYS, f"{kind} did not return a Test_Result_Contract dict"

    if kind == KIND_SUCCESS:
        assert result["execution_successful"] is True
        assert result["executed"] is True
        assert result["exit_code"] == 0
    elif kind in {KIND_STATEMENT_ERROR, KIND_STATEMENT_BAD_STATE}:
        # It RAN and failed: `executed` true is what distinguishes these from
        # every route below.
        assert result["executed"] is True
        assert result["execution_successful"] is False
        assert result["exit_code"] != 0
    else:
        assert result["executed"] is False
        assert result["execution_successful"] is False
        assert result["exit_code"] is None

    if kind in HARNESS_KINDS:
        assert result.get("not_verified_by_harness") is True
    else:
        assert "not_verified_by_harness" not in result


def _classify(result: dict[str, Any]) -> str:
    """Which route *result* came out of, read off the contract fields alone.

    Coarse on purpose — it distinguishes the four groups a caller can tell apart
    without reading prose, which is all
    :func:`test_every_variant_lands_on_the_kind_it_names` needs to catch a
    variant that fell through to a different builder.
    """
    if result["execution_successful"]:
        return "ran-and-passed"
    if result["executed"]:
        return "ran-and-failed"
    if result.get("not_verified_by_harness"):
        return "harness-non-verification"
    return "never-ran-failure"


_EXPECTED_GROUP: dict[str, str] = {
    KIND_SUCCESS: "ran-and-passed",
    KIND_STATEMENT_ERROR: "ran-and-failed",
    KIND_STATEMENT_BAD_STATE: "ran-and-failed",
    KIND_FRAME_REFUSAL: "harness-non-verification",
    KIND_FRAME_UNBUILDABLE: "harness-non-verification",
    KIND_CAPACITY: "harness-non-verification",
    KIND_CREATE_FAILURE: "never-ran-failure",
    KIND_CLIENT_UNAVAILABLE: "never-ran-failure",
    KIND_LIFECYCLE_FAILURE: "never-ran-failure",
    # p44's tenth. Both doors land here: `executed` false with the harness marker
    # is the whole of R2.2 — a failed source read is not a transform that ran and
    # failed. `_classify` cannot tell the two doors apart, which is what
    # `test_both_sample_read_doors_reach_the_source_read_reason` is for.
    KIND_SAMPLE_READ_FAILURE: "harness-non-verification",
}


def test_the_expected_group_map_is_total_over_every_kind():
    """Guard on :data:`_EXPECTED_GROUP`: every kind has an expectation.

    p44 task 7.1's addition, and it is the assertion that would have failed when
    the tenth outcome landed if the map had been consulted with a ``.get``.
    :func:`test_every_variant_lands_on_the_kind_it_names` indexes it directly, so
    a missing kind is a ``KeyError`` rather than a silent pass — but a ``KeyError``
    inside a hypothesis draw reads as a test bug, not as "the enumeration is
    incomplete", so the completeness claim is stated here where it is legible.
    """
    assert set(_EXPECTED_GROUP) == set(ALL_KINDS), (
        "every kind needs a claimed route group, or its draws assert nothing about where they landed"
    )
    assert len(ALL_KINDS) == len(set(ALL_KINDS)) == 10, (
        "ALL_KINDS enumerates `run_test`'s reachable outcomes; re-read its routing before changing the count"
    )


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(outcome=_outcomes())
def test_every_variant_lands_on_the_kind_it_names(outcome: Outcome):
    """Every *variant* of every kind reaches the route its kind claims.

    Not a restatement of :func:`test_the_generator_reaches_every_outcome_kind`,
    which drives each kind once at its **default** variant. This drives the
    variant axes too, and it is the check that earns its place: it caught a draft
    in which ``CANCELLING`` / ``TIMEOUT`` / ``""`` were generated as statement
    states, stalled ``_await_statement``'s poll loop, and silently arrived at the
    creation-failure builder instead. Property 4 passed on those draws while
    testing nothing it claimed to.

    A property whose generator has drifted is worse than no property, because it
    reports coverage it does not have.
    """
    result = _drive(outcome)

    assert _classify(result) == _EXPECTED_GROUP[outcome.kind], (
        f"the {outcome.kind!r} variant {outcome!r} reached the "
        f"{_classify(result)!r} route, so that draw does not exercise the outcome it names"
    )


@pytest.mark.parametrize("door", _SAMPLE_READ_DOORS)
def test_both_sample_read_doors_reach_the_source_read_reason(door: str):
    """Each door of the tenth kind reaches its own builder (p44 R2.1, R2.2, R3.4).

    The reachability guard for the variant axis p44 task 7.1 added, and it exists
    because :func:`_classify` is coarse by design: both doors are harness
    non-verifications, so :func:`test_every_variant_lands_on_the_kind_it_names`
    would stay green if the ``deadline`` draws quietly stopped taking the deadline
    arm and joined the statement ones. That is
    :data:`_BAD_STATEMENT_STATES`' trap one axis further in — a draw passing while
    exercising something other than what it names.

    The two builders are distinguished by the sentence each writes, which is the
    only observable difference in the contract dict: they carry the same key set,
    the same reason and the same falsy ``executed`` / ``exit_code``. Asserted as a
    substring of ``error_details`` rather than as its whole value, because the
    exact wording is
    ``test_sample_read_failure_builders.py``'s to own — this only needs to tell
    the two doors apart.
    """
    result = _drive(Outcome(kind=KIND_SAMPLE_READ_FAILURE, sample_read_door=door))

    assert result["not_verified_by_harness"] is True
    assert result["not_verified_reason"] == NOT_VERIFIED_REASON_SOURCE_READ_FAILED
    assert result["executed"] is False, "a failed source read is not a transform that ran (p44 R2.2)"
    assert result["exit_code"] is None
    assert SAMPLE_SOURCE_URI in (result["error_details"] or ""), (
        "the reported detail must name the object whose read failed"
    )

    deadline_named = "within the statement deadline" in (result["error_details"] or "")
    assert deadline_named is (door == DOOR_DEADLINE), (
        f"the {door!r} door produced {result['error_details']!r}, which is the other door's "
        f"builder — so that draw does not exercise the route it names"
    )


def test_the_harness_marker_is_reachable():
    """The absence of the proxy marker is a discrimination, not a silence.

    **Validates: Requirement 3.2**

    This is what stops Property 4 from being vacuously true. The runner reaches
    a non-verification on four paths, and each carries the **harness** marker
    with a reason — the frame refusal's width/no-schema reasons, capacity's
    ``session_capacity_exhausted`` (R8.1), the frame-unbuildable
    ``test_frame_unbuildable`` (R7.3) and p44's ``source_read_failed`` (p44 R2.1,
    R2.2). So a result can and does carry ``not_verified_reason``; what it never
    carries is the proxy attribution.

    ``LGR-b2f79ef-2`` is why the same two-key encoding is reused for a fifth
    reason instead of a new field being added.

    **The distinct-reason assertion is the one p44 task 7.1 had to keep exact**
    (``LGR-60c4985-6``). It holds iff the new kind and the new reason landed
    together: adding :data:`KIND_SAMPLE_READ_FAILURE` to :data:`HARNESS_KINDS`
    without ``source_read_failed`` existing would leave three distinct reasons
    against four kinds and fail here, which is the assertion's whole job. Relaxing
    it to ``>=`` or to a subset test would make it hold after a reason was
    *deleted* — the thing it exists to notice.
    """
    reasons: dict[str, str] = {}
    for kind in sorted(HARNESS_KINDS):
        result = _drive(Outcome(kind=kind))
        assert result["not_verified_by_harness"] is True, f"{kind} lost the harness marker"
        assert "not_verified_reason" in result, f"{kind} carries no reason a reviewer can read"
        assert "not_verified_by_proxy" not in result, f"{kind} attributed a harness refusal to the proxy"
        reasons[kind] = result["not_verified_reason"]

    # Four distinct reasons, so the shared `not_verified_reason` vocabulary is
    # demonstrably exercised rather than assumed reachable.
    assert reasons[KIND_CAPACITY] == NOT_VERIFIED_REASON_SESSION_CAPACITY
    assert reasons[KIND_FRAME_UNBUILDABLE] == NOT_VERIFIED_REASON_FRAME_UNBUILDABLE
    assert reasons[KIND_SAMPLE_READ_FAILURE] == NOT_VERIFIED_REASON_SOURCE_READ_FAILED
    assert len(set(reasons.values())) == len(HARNESS_KINDS), (
        "two harness refusals reported the same reason, so the report cannot say which happened"
    )


# ---------------------------------------------------------------------------
# The cause: no converter is reachable from this module at all
# ---------------------------------------------------------------------------
#
# A property over outcomes cannot see an import that is present but not yet
# called — it produces no marker today and is exactly the edit R3.2 needs to
# fail on. So the cause is asserted too, on the module NAMESPACE.
#
# Never on source text. `LGR-b46a5bb-4` rejected substring matching for this
# repo's other absence invariant because "it forbids documenting the fence, and
# failed for exactly that reason against handlers/agents.py, whose docstring
# names threading.Thread on purpose". Live here: `run_test`'s docstring names
# `PySpark2PandasConverter` and `not_verified_by_proxy` deliberately, to record
# why the absence is the feature.


def _converter_symbols() -> tuple[str, ...]:
    """Every symbol ``pyspark_to_pandas`` itself defines, publicly.

    Module objects are filtered out, and that filter is the difference between
    a real check and a flaky one: ``dir()`` reports the converter's own
    ``import ast`` / ``re`` / ``textwrap`` / ``warnings``, and forbidding those
    would fail the moment ``glue_session_runner`` imported ``re`` for a reason
    with nothing to do with R3.2.
    """
    return tuple(
        name
        for name in sorted(dir(pyspark_to_pandas))
        if not name.startswith("_") and not isinstance(getattr(pyspark_to_pandas, name), types.ModuleType)
    )


def test_the_converter_symbol_list_is_not_empty():
    """Guard: the symbol list the property quantifies over is non-empty.

    An empty list makes :func:`test_no_converter_symbol_is_bound_in_the_runner`
    pass without checking anything, and the filter above is exactly the kind of
    expression that can start returning nothing after an unrelated edit.
    """
    symbols = _converter_symbols()
    assert symbols, "the converter exports nothing, so the cause property is vacuous"
    assert "PySpark2PandasConverter" in symbols, "the converter class is what R3.2 is about"
    assert "ConversionError" in symbols, "the refusal exception is the marker's producer"


@given(name=st.sampled_from(_converter_symbols()))
@settings(max_examples=50, deadline=None)
def test_no_converter_symbol_is_bound_in_the_runner(name: str):
    """R3.2 at its root, generalised: no converter symbol is in the namespace.

    **Validates: Requirement 3.2**

    The consequence (no result carries the marker) holds because of this. Task
    8.1's ``test_the_module_imports_no_converter`` asserts it for two named
    symbols; this quantifies over every symbol the converter module defines, so
    a future edit that imported a third "just for a helper" fails here with the
    requirement attached rather than passing until it is called.
    """
    assert name not in vars(glue_session_runner), (
        f"glue_session_runner binds the converter symbol {name!r}. R3.2 forbids a proxy "
        f"non-verification on any Glue result, and the absence of a proxy is what makes "
        f"that true by construction rather than by assertion."
    )


def test_the_converter_module_is_not_bound_in_the_runner():
    """The module itself is not reachable either, under any alias.

    Checking the symbols alone would miss ``import pyspark_to_pandas`` followed
    by ``pyspark_to_pandas.PySpark2PandasConverter(...)`` at a call site, which
    binds no converter symbol in the namespace while making every one of them
    reachable.
    """
    bound_modules = {
        alias: value.__name__
        for alias, value in vars(glue_session_runner).items()
        if isinstance(value, types.ModuleType)
    }
    assert "pyspark_to_pandas" not in bound_modules.values(), (
        f"glue_session_runner binds the converter module (as {bound_modules}) — every proxy "
        f"symbol is reachable through it, so R3.2 would hold only by nobody calling it"
    )
