# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Feature: p44-verify-against-real-source-data, Property 3: The shadow half is
# always synthetic
"""Property 3 — the shadow half is always synthetic.

Spec ``p44-verify-against-real-source-data`` task 10.2, design §4.2.

    *For any* configured frame mode, the Glue runner the shadow path constructs
    receives ``FRAME_MODE_SYNTHETIC``, and no shadow run reaches a Sample_Frame.

**Validates: Requirements 1.6**

WHAT THIS ADDS OVER TASK 10.1's EXAMPLE, AND WHY THE TWO ARE NOT THE SAME CLAIM
-------------------------------------------------------------------------------
``test_shadow_orchestration.py::test_the_default_factory_constructs_the_glue_runner_with_its_four_arguments``
(amended by task 10.1, and the amendment is a reversal rather than a regression
— ``LGR-60c4985-6``) calls :func:`lambda_function._default_shadow_glue_runner`
**directly** with three arguments and asserts the four kwargs it forwards. That
is one example on the factory's own signature, and it is the right shape for
what it pins.

This file's contribution is the **quantifier, one level up**:

* the drive goes through :func:`lambda_function.record_shadow_verification` with
  ``runner_factory=None``, so the factory under test is the one the *handler*
  reaches rather than one the test hands in;
* the axis is *any configured frame mode* — the ``VERIFICATION_FRAME_MODE``
  variable task 9.1 wired, drawn over the whole ``str | None`` domain including
  ``"sample"`` itself, both near-miss families and the unset state;
* and "no shadow run reaches a Sample_Frame" is asserted **through the real
  classifier** rather than as a string comparison, by feeding the mode read back
  out of the recorded construction into
  :meth:`GlueSessionTestRunner.resolve_frame_source` over a sample-capable URI.

Nothing here re-asserts the factory's kwarg list or the function-local import;
those are 10.1's subject next door.

WHY THE ``"sample"`` DRAW IS THE INTERESTING ONE
------------------------------------------------
``VERIFICATION_FRAME_MODE`` is ``'sample'`` in **both** deployed stacks
(``compute-stack.ts`` and ``agentcore-stack.ts``, tasks 15.1 and 15.2), so the
configuration this property has to hold under is not hypothetical: it is the
shipped one. A shadow run in a deployment configured for sampling is precisely
the case R1.6 exists for, and it is the draw that fails if anyone later "fixes"
the shadow half to honour the environment.

WHY THE PIN EXISTS AT ALL — PARITY IS UNIMPLEMENTABLE, NOT UNIMPLEMENTED
------------------------------------------------------------------------
The pandas half of a shadow comparison is
``CodeInterpreterTestRunner(region=AWS_REGION)`` (``lambda_function.py:2712``),
whose ``__init__`` signature is exactly ``(self, region)`` — verified by task
5.2 — with one ``bedrock-agentcore`` client and **no** ``frame_mode``, **no**
``source_data_uri`` and no S3 reference of any kind. Its frame is generated
inside the interpreter from the *declared* schema. So a real sample on the Glue
half would compare real rows against generated ones, and every data-quality
dimension would disagree for a reason that has nothing to do with either
executor — the one thing shadow mode exists to measure. Giving the pandas half a
source read is a change to a second executor and out of p44's scope, so the door
is shut rather than missing: a Sample_Frame is reachable only under the
non-shadow ``glue`` executor.

THE PIN LIVES IN THE FACTORY, NOT AT THE CALL SITE (do-NOT 22)
--------------------------------------------------------------
``record_shadow_verification`` calls ``factory(region=…, role_arn=…,
source_data_uri=…)`` (``lambda_function.py:1110-1114``) and its
``runner_factory`` parameter is a test seam. Passing ``frame_mode`` there would
put the pin somewhere an injected factory can be handed ``sample``.
:class:`TestThePinLivesInTheFactoryNotTheCallSite` asserts that the seam is
never handed one — the complement of the property above, and the reason the two
have to be separate assertions: the first says the *default* factory pins, the
second says the *call site* does not decide.

NO GLUE SESSION IS CREATED ANYWHERE IN THIS FILE, AND NONE MAY BE
-----------------------------------------------------------------
do-NOT 1 — interactive sessions bill, and task 5.1's subagent reached a real
``CreateSession`` from an in-process exercise, so this is measured rather than
asserted in prose. Three measures:

1. **The drive never constructs a real runner.**
   ``GlueSessionTestRunner`` is patched **on the real module object** for the
   duration of every drive, so the factory's function-local
   ``from glue_session_runner import FRAME_MODE_SYNTHETIC, GlueSessionTestRunner``
   binds a ``MagicMock`` for the class while ``FRAME_MODE_SYNTHETIC`` stays the
   real ``"synthetic"``. That is a deliberate difference from 10.1, which stubs
   the whole module and therefore compares against ``stub.FRAME_MODE_SYNTHETIC``:
   keeping the module real is what lets this file feed the observed value to the
   real classifier instead of to a mock.
2. **The classifier half constructs a real runner over a client that cannot be
   used** — :class:`RaisingGlueClient`, and it calls only
   ``resolve_frame_source``, which R1.5 requires to reach its decision with no
   AWS API at all.
3. **``botocore``'s own call seam is guarded** by :func:`no_glue_api_calls`, the
   reusable form task 6.2 introduced. A client neither patch can see — one built
   at import, or through ``boto3.Session().client``, or because a patch target
   moved — hits the guard instead of AWS.
   :meth:`TestTheHarnessIsNotFailingOpen.test_the_glue_api_guard_is_armed`
   provokes it on purpose, so measure 3 is itself measured.

Measured once below all three, at the transport: this file was run with
``botocore.httpsession.URLLib3Session.send`` replaced by a raiser, which is where
*any* outbound AWS call has to pass — including one from a client no patch here
can see. All tests still passed, so no AWS API was reached rather than none was
reached by the routes somebody thought of.

``make test-e2e`` and the two ``tests/e2e/test_glue_*.py`` files are forbidden
for this spec, and ``--override-ini=addopts=`` is never passed by hand.

THE NON-VACUITY ARGUMENT
------------------------
"The recorded ``frame_mode`` is synthetic" is vacuously true if no runner was
constructed, if ``frame_mode`` never reached the kwargs, or if the URI could
never have produced a sample anyway. All four holes are closed:

* :func:`_assert_pinned_to_synthetic` requires **exactly one** construction and
  ``frame_mode`` present **as a keyword**, so a shadow half that declined to run
  — or a positional-argument refactor — fails there instead of emptying the
  assertion;
* every drive asserts ``SHADOW_STATUS_COMPARED`` and one written row, so the
  Glue half genuinely ran and was compared;
* :meth:`TestTheHarnessIsNotFailingOpen.test_the_sample_corpus_would_really_reach_a_sample`
  shows every URI in :data:`SAMPLE_CAPABLE_URIS` resolves to ``is_sample`` True
  under an explicit ``sample`` mode, so ``is_sample`` False on the shadow path is
  attributable to the pin and not to an unsampleable URI;
* :meth:`TestTheHarnessIsNotFailingOpen.test_the_checker_rejects_a_construction_that_honoured_the_configuration`
  provokes the checker with a construction carrying ``frame_mode="sample"`` and
  requires it to fail, so the assertion is known to have teeth. That control
  builds the wrong construction by hand; **nothing here requires production to
  pass ``frame_mode`` through the seam**, which do-NOT 22 forbids.

**One draw in this domain is vacuous, and it is why ``@example`` is stacked
rather than trusted to the seed.** A configured value of ``"synthetic"``, ``""``
or unset produces the same ``frame_mode`` under the pin as it would under a
factory that *honoured* the configuration, so those draws cannot distinguish the
two. Measured, not reasoned: a deliberately wrong factory reading
``VERIFICATION_FRAME_MODE`` was driven through this harness and caught on every
non-synthetic value and on none of the falsy ones. The discriminating draws are
therefore pinned by ``@example`` — ``"sample"`` first, because that is the value
both stacks deploy — instead of left to a 200-draw sample that reaches them with
high probability but not certainly.

WHAT THIS FILE DOES NOT DUPLICATE
---------------------------------
* ``test_shadow_orchestration.py`` (p42 tasks 10.3/10.5, amended by 10.1) owns
  the factory's kwarg list by example, the row shape, the float→Decimal
  conversion, fail-soft at four raise sites, and the deadline in both
  directions. Not touched by this task.
* ``test_shadow_verdict_inertness_prop.py`` (Property 15) owns "shadow cannot
  change a verdict" and ``patch.object``s the factory wholesale, so it never
  observes the production one. Nothing here asserts a verdict or a check list.
* ``test_frame_mode_caller_prop.py`` (task 9.3, Property 2) owns the ``glue``
  branch's read: unnormalised, per invocation, and the WARNING the classifier
  emits. This file asserts nothing about that read and captures no logs — the
  shadow path is required to be **insensitive** to the value, which is the
  opposite claim.
* ``test_frame_mode_resolution_prop.py`` (task 9.2, Property 1) owns the
  classifier's totality and the ``s3://`` gate. It is used here as an oracle for
  one direction only, over a fixed corpus.
* The three-statements-per-session bound stays untouched: no session statement is
  added anywhere by this task (do-NOT 9, design C7), so
  ``SHADOW_GLUE_WORST_CASE_SECONDS`` and the ``3 ×`` in its drift fence
  (``test_shadow_orchestration.py:229``) are unaffected.

Ledger notes — including where the ledger is silent
---------------------------------------------------
* ``why`` on ``lambda_function.py`` returns five commits (``5108dd4``,
  ``0fe3e5d``, ``42dbdb1``, ``5c30733``, ``ad78946``) and **no record naming
  ``_default_shadow_glue_runner`` or ``record_shadow_verification``**. Task 10.1
  reported the same. Stated as a gap in the ledger, not as an all-clear.
* ``why`` at ``:791`` (``SHADOW_GLUE_WORST_CASE_SECONDS``, the nearest committed
  line to this area) blames ``ad78946`` and returns its five records, all
  carrying ``NOTE: BROAD`` over 60 files. Read past them: none is a fence on the
  shadow orchestration. A line-scoped ``why`` on the factory itself is
  **unavailable** — 10.1's edit is uncommitted, so ``git blame`` yields the
  all-zero SHA and the tool errors out. That is a tooling limit, not an absence
  of history.
* ``LGR-819980f-1`` (**standing invariant**) is why the sample path is
  ``s3://``-only, and it is upheld here structurally rather than by assertion:
  every drive that resolves a frame does so through the real
  ``resolve_frame_source``, whose ``FRAME_MODE_SAMPLE`` return is guarded by
  ``is_s3_source_uri``.
* ``LGR-b46a5bb-1`` (**standing invariant**, concept ``api``) — no thread for
  post-response work in ``lambda/**``. Honoured trivially: this file adds no
  production code and the drive is inline.
* ``LGR-ad78946-1`` (**template / ratified**, ``NOTE: BROAD``) — the
  no-normalisation fence for the executor vocabulary. Cited only to say what this
  file does **not** do: it draws near-misses to prove the shadow path ignores
  them, and leaves the naming-at-WARNING claim to task 9.3.
* ``LGR-3e45e65-1`` (**template**, concept ``testing``) — the unit layer pins
  ``AWS_REGION`` in ``tests/unit/conftest.py`` by assignment, so the
  ``setdefault`` calls below are consistent no-ops, kept for module-import parity
  with every sibling in this directory.
* ``LGR-b46a5bb-4`` (**template**, concept ``testing``) — substring matching was
  rejected for testing an invariant because "it forbids documenting the fence".
  This file asserts behaviour: no assertion reads the module's source text.
* ``decisions(concept="testing", include_backfilled=True)`` returns six records,
  of which the two above bear on this file's *form* rather than its subject.
  ``decisions(concept="performance", include_backfilled=True)`` returns one
  (``LGR-9e76249-2``, read-path derivation), which does not govern here.
  ``decisions(concept="api")`` scoped to this Lambda returns two, one of them the
  thread invariant. All three areas are thin here rather than settled.
* ``LGR-4dd9b42-1`` **does** resolve through ``record(id)`` — task 10.1 could not
  surface it in any payload, and the direct fetch works. Read in full: its
  subject is the pandas-proxy literal-cast converter, so it governs nothing in
  this task. Recorded because "could not surface it" and "it does not apply" are
  different facts.

CodeGraph, for the symbols this drives
--------------------------------------
* ``record_shadow_verification`` (``lambda_function.py:1015``) — 1 caller,
  ``lambda_handler``; ⚠️ "no covering tests found". **That flag is a false
  negative**, and task 10.1 said so first: ``test_shadow_orchestration.py``
  drives this function throughout via its ``_record`` helper, and
  ``test_shadow_verdict_inertness_prop.py`` drives it on every example. The
  documented cause is ``sys.path`` manipulation plus bare-name imports the symbol
  graph does not follow.
* ``_default_shadow_glue_runner`` (``:958``) — **no blast-radius line was emitted
  for it at all** across two ``explore`` calls, so neither a caller count nor a
  coverage flag is available from the tool. Grep over ``lambda/`` and ``shared/``
  gives the production figure: 1 caller, ``record_shadow_verification:1105``.
* ``GlueSessionTestRunner`` (``glue_session_runner.py:535``) — reported as "47
  callers in ``lambda_function.py``"; the production figure is **2** (``:1007`` in
  the shadow factory, ``:3083`` in the ``glue`` branch), the rest being test sites
  the graph attributes to the module. 20 covering test files. ``__init__``
  (``:593``) got no separate line of its own.

``glue_session_runner`` imports nothing from ``shared/`` — only stdlib, ``boto3``
and its sibling ``code_interpreter_runner`` — and the handler's ``shared/``
imports are stubbed here exactly as in every sibling, so the shared-layer blast
radius of this task is nil.
"""

import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import botocore.client
import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

# Region/account before any boto3 import. LGR-3e45e65-1 pins the region in
# tests/unit/conftest.py, so these are consistent no-ops.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# The stub set every sibling in this directory uses. `shadow_comparison` is NOT
# stubbed: the orchestration is only meaningful against the real comparison,
# which is a pure function and needs nothing. `glue_session_runner` is not
# stubbed either — this file needs the REAL module, both for its vocabulary and
# for its classifier, and patches only the runner class on it.
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

import glue_session_runner


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Sibling files import the handler under their own stubs and rebind the name."""
    sys.modules["lambda_function"] = _mod
    yield


@pytest.fixture(autouse=True)
def _ensure_the_real_glue_module_is_importable_by_name():
    """Pin ``sys.modules["glue_session_runner"]`` to the real module.

    Not belt and braces. The factory's import is function-local
    (``from glue_session_runner import …``), so it resolves the NAME through
    ``sys.modules`` at call time — and a sibling in this directory registers a
    ``MagicMock`` under that name for the duration of its own drives
    (``test_frame_mode_caller_prop.py``). Patching an attribute on the module
    object this file holds would then be invisible to the factory, and the
    ``frame_mode`` read back would be a ``MagicMock`` attribute rather than the
    real ``"synthetic"`` — which is the one thing this file must not compare
    against.
    """
    sys.modules["glue_session_runner"] = glue_session_runner
    yield


ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

_SETTINGS = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# ══════════════════════════════════════════════════════════════════════
# do-NOT 1, measured rather than assumed
# ══════════════════════════════════════════════════════════════════════


class GlueClientTouched(BaseException):
    """The classifier reached for the Glue client. R1.5 says it must not.

    ``BaseException`` on purpose: an ``Exception`` here would be caught by
    ``record_shadow_verification``'s inner classifier arm — which catches
    everything by design (R4.4) — and recorded as "the Glue half produced
    nothing", turning a violated requirement into a passing test.
    """


class RaisingGlueClient:
    """A stand-in for the boto3 Glue client that raises on **any** use.

    Copied from ``test_frame_mode_resolution_prop.py`` (task 9.2), which is the
    reusable form, rather than reinvented — and it is a stronger measure than a
    ``MagicMock``, which would answer every call and reduce the AWS-free claim to
    "these particular attributes were not touched", checked after the fact
    against an enumeration somebody had to remember.

    Deliberately raises on *use* and not on construction:
    ``GlueSessionTestRunner.__init__`` catches everything out of ``boto3.client``
    and falls back to ``self.client = None`` (``glue_session_runner.py:603-610``),
    so a client that failed to build would make the claim vacuous.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        raise GlueClientTouched(
            f"resolve_frame_source reached the Glue client (`.{name}`) — R1.5 requires the frame "
            f"decision to be reached with no AWS API call, and p44 do-NOT 1 forbids a session."
        )


class GlueApiGuardViolation(BaseException):
    """A test in this file reached a real Glue API call. Patch the seam."""


#: The session operations that bill, plus the polls a run would make against a
#: live session. Enumerated rather than "every Glue operation" so the guard's
#: message can name what was attempted. Same set as
#: ``test_sample_read_failure_prop.py``'s, deliberately.
GUARDED_GLUE_OPERATIONS = frozenset(
    {"CreateSession", "GetSession", "RunStatement", "GetStatement", "StopSession", "DeleteSession"}
)

_REAL_MAKE_API_CALL = botocore.client.BaseClient._make_api_call


@pytest.fixture(autouse=True)
def no_glue_api_calls(monkeypatch):
    """Make a real Glue session call unreachable from this file (do-NOT 1).

    The backstop behind the two construction-seam patches, for a client neither
    can see. ``BaseException`` on purpose — see the module docstring's measure 3.
    """

    def _guarded_make_api_call(self, operation_name, api_params):
        if operation_name in GUARDED_GLUE_OPERATIONS:
            raise GlueApiGuardViolation(
                f"a unit test reached a real Glue {operation_name} call — p44 do-NOT 1 forbids creating "
                f"an interactive session, which bills. Patch the construction seam before constructing a "
                f"runner rather than weakening this guard."
            )
        return _REAL_MAKE_API_CALL(self, operation_name, api_params)

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", _guarded_make_api_call)


# ══════════════════════════════════════════════════════════════════════
# The configured values, and the URIs a sample could be read from
# ══════════════════════════════════════════════════════════════════════

#: The recognised pair, spelled out rather than read off the module, then checked
#: against it by :func:`_assert_the_vocabulary_still_matches_the_module`. A
#: generator that follows the code it tests cannot detect the code moving.
_VOCABULARY = ("synthetic", "sample")

#: Values a deployment might carry, near-misses included. The corpus is task
#: 9.3's, trimmed: this file does not assert on warnings, so it needs the shapes
#: rather than the families that separate a trimmable near-miss from an interior
#: one. ``"sample"`` heads the list because it is the value both stacks actually
#: deploy (tasks 15.1, 15.2) and therefore the draw that matters.
_NAMED_CONFIGURED_VALUES = (
    "sample",
    "synthetic",
    "Sample",
    "sample ",
    " sample",
    "SAMPLE",
    "samples",
    "smaple",
    "",
    " ",
    '"sample"',
    "'sample'",
    "['sample']",
    "sample,synthetic",
    "true",
    "false",
    "none",
    "None",
    "null",
    "real",
    "glue",
    "pandas",
)

#: Characters safe to put in ``os.environ``. NUL is excluded because
#: ``os.environ[k] = v`` raises ``ValueError: embedded null byte``, and
#: surrogates because they cannot be encoded — a draw of either would fail this
#: file's harness rather than its subject.
_ENV_SAFE_CHARACTERS = st.characters(codec="utf-8", exclude_categories=("Cs", "Cc"))

#: The whole declared domain of the configuration: ``str | None``, where ``None``
#: is the variable being **unset** — a distinct state from ``""``, because the
#: read is ``os.environ.get(ENV) or DEFAULT``.
_arbitrary_configured = st.one_of(
    st.none(),
    st.sampled_from(_NAMED_CONFIGURED_VALUES),
    st.text(alphabet=_ENV_SAFE_CHARACTERS, max_size=12),
)

#: URIs that a ``sample`` mode genuinely WOULD read: ``s3://`` with a non-empty
#: bucket and an extension ``sample_read_format`` resolves, one per canonical
#: format plus a compressed one. This corpus is what makes the "no Sample_Frame"
#: half of the property non-vacuous, and
#: :meth:`TestTheHarnessIsNotFailingOpen.test_the_sample_corpus_would_really_reach_a_sample`
#: is where that is measured rather than assumed.
SAMPLE_CAPABLE_URIS = (
    "s3://adi-source-files-123456789012-us-west-2/uploads/customers.csv",
    "s3://adi-source-files-123456789012-us-west-2/uploads/orders.json",
    "s3://adi-source-files-123456789012-us-west-2/uploads/events.ndjson",
    "s3://adi-source-files-123456789012-us-west-2/uploads/facts.parquet",
    "s3://adi-source-files-123456789012-us-west-2/uploads/legacy.avro",
    "s3://adi-source-files-123456789012-us-west-2/uploads/legacy.orc",
    "s3://adi-source-files-123456789012-us-west-2/uploads/customers.csv.gz",
)

#: URIs a sample could not be read from anyway — the ordinary shapes the wiring
#: produces. Drawn alongside the capable ones so the property covers the whole
#: URI axis and not only the interesting end of it. ``jdbc://pipeline/<id>`` is
#: ``schema_extract``'s own fallback and is structurally unreachable for a read
#: (``LGR-819980f-1``); ``s3a://`` is refused by the literal-prefix gate.
SAMPLE_INCAPABLE_URIS = (
    None,
    "",
    "jdbc://pipeline/pipe-1",
    "jdbc://postgres/public/customers",
    "s3a://adi-source-files-123456789012-us-west-2/uploads/customers.csv",
    "s3://adi-source-files-123456789012-us-west-2/uploads/",
    "s3://adi-source-files-123456789012-us-west-2/uploads/README",
)

_sample_capable = st.sampled_from(SAMPLE_CAPABLE_URIS)
_any_source_uri = st.one_of(st.sampled_from(SAMPLE_CAPABLE_URIS), st.sampled_from(SAMPLE_INCAPABLE_URIS))


def _over_the_discriminating_configurations(test):
    """Stack one ``@example`` per configured value that can observe a regression.

    See the module docstring's non-vacuity section: a falsy or already-synthetic
    draw produces the same ``frame_mode`` whether the pin is honoured or the
    configuration is, so only a non-synthetic value distinguishes them.
    ``"sample"`` leads because it is what both stacks deploy; the two near-misses
    follow because a future ``.strip()`` or ``.lower()`` at the read site would
    turn them into ``"sample"`` and they must stay pinned either way.

    The URI is the first sample-capable one, so each example is also the shape
    where a leaked ``sample`` would actually reach a read.
    """
    for value in ("sample ", "Sample", "sample"):
        test = example(configured=value, source_data_uri=SAMPLE_CAPABLE_URIS[0])(test)
    return test


# ══════════════════════════════════════════════════════════════════════
# The drive
# ══════════════════════════════════════════════════════════════════════

SOURCE_SCHEMA = {"fields": [{"name": "id", "type": "string"}, {"name": "name", "type": "string"}]}
TARGET_SCHEMA = {"properties": {"id": {"type": "string"}, "name": {"type": "string"}}}

#: A pandas-proxy result trimmed to the dimensions ``shadow_comparison``
#: compares, so the comparison has something real to do and the row is written.
PANDAS_RESULT = {
    "execution_successful": True,
    "executed": True,
    "exit_code": 0,
    "output_records": 100,
    "data_quality_results": {
        "duplicates": 0,
        "schema_compliance": 100.0,
        "missing_columns": [],
        "null_counts": {"id": 0, "name": 0},
    },
}

#: The Glue half's answer. A real contract-shaped dict rather than a bare
#: ``MagicMock``: ``compare_results`` and the DynamoDB float conversion both walk
#: this value, and a mock would route the drive into the outer fail-soft arm —
#: which still records a construction, so every assertion below would pass while
#: the orchestration was silently broken.
GLUE_RESULT = {
    "execution_successful": True,
    "executed": True,
    "exit_code": 0,
    "output_records": 100,
    "data_quality_results": {
        "duplicates": 0,
        "schema_compliance": 100.0,
        "missing_columns": [],
        "null_counts": {"id": 0, "name": 0},
    },
}


class _Recorder:
    """A writer seam that keeps what it was handed."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def __call__(self, item: dict) -> None:
        self.items.append(item)


def _stepped_clock(*readings):
    """A monotonic-shaped clock returning each reading in turn, then the last.

    Nothing in this file waits: every reading is injected, which is what
    ``record_shadow_verification``'s ``clock`` parameter exists for.
    """
    values = list(readings)

    def clock():
        return values.pop(0) if len(values) > 1 else values[0]

    return clock


def _shadow_run(configured, source_data_uri, *, runner_factory=None):
    """Drive one shadow verification and return ``(status, constructor, writer)``.

    ``runner_factory`` defaults to ``None``, which is the point: the orchestration
    then reaches :func:`lambda_function._default_shadow_glue_runner`, so what is
    under test is the **production** factory and not one this test injected.

    *configured* is the ``VERIFICATION_FRAME_MODE`` value, or ``None`` for the
    variable being unset. ``patch.dict`` adds and overrides but never removes,
    and it restores the whole mapping on exit, so popping inside it is how
    "unset" is expressed and it is safe.

    The deadline is passed explicitly rather than left to the environment, so no
    drive can be turned into an abandonment by an ambient
    ``SHADOW_VERIFICATION_DEADLINE_SECONDS``.
    """
    writer = _Recorder()
    runner = MagicMock()
    runner.run_test.return_value = dict(GLUE_RESULT)
    constructor = MagicMock(return_value=runner)

    env = {} if configured is None else {_mod.VERIFICATION_FRAME_MODE_ENV: configured}
    with (
        patch.object(glue_session_runner, "GlueSessionTestRunner", constructor),
        patch.dict("os.environ", env),
    ):
        if configured is None:
            os.environ.pop(_mod.VERIFICATION_FRAME_MODE_ENV, None)
        status = _mod.record_shadow_verification(
            run_id="run-1",
            pipeline_id="pipe-1",
            transform_block="df = df",
            source_schema=SOURCE_SCHEMA,
            target_schema=TARGET_SCHEMA,
            pandas_result=PANDAS_RESULT,
            source_data_uri=source_data_uri,
            session_role_arn=ROLE_ARN,
            step_started_at=0.0,
            clock=_stepped_clock(1.0),
            deadline_seconds=900,
            runner_factory=runner_factory,
            writer=writer,
        )
    return status, constructor, writer


def _assert_the_shadow_half_actually_ran(status, writer):
    """The floor under every assertion here.

    ``record_shadow_verification`` never raises (R4.4), so a broken drive returns
    a label instead of failing — and every "the construction carried synthetic"
    claim would then be about a construction that never happened. Asserted on the
    status **and** on the written row, because the fail-soft arm returns a label
    without writing one.
    """
    assert status == _mod.SHADOW_STATUS_COMPARED, (
        f"the shadow half returned {status!r} rather than {_mod.SHADOW_STATUS_COMPARED!r}, so the Glue "
        f"half did not run and was not compared — every assertion about its construction would be vacuous"
    )
    assert len(writer.items) == 1, f"expected exactly one shadow row, got {len(writer.items)}"


def _assert_pinned_to_synthetic(constructor, *, configured, source_data_uri):
    """Return the ``frame_mode`` the runner was constructed with, pinned synthetic.

    **The two anti-vacuity guards live here**, so every caller inherits them:
    exactly one construction, and ``frame_mode`` present **as a keyword** — a
    positional-argument refactor would otherwise make the kwargs read return
    nothing and every assertion downstream trivially true.

    Compared against ``glue_session_runner.FRAME_MODE_SYNTHETIC`` and, second,
    against the literal ``"synthetic"``. Both, because each catches a different
    thing: the constant is the runner's own vocabulary, so the factory taking its
    value from there is what R1.6 asks for; the literal is what stops a rename of
    the constant from silently satisfying this against a different string.
    """
    calls = constructor.call_args_list
    assert len(calls) == 1, (
        f"expected exactly one runner construction, got {len(calls)} — the shadow half's Glue side did "
        f"not run once, so this property would be vacuous"
    )
    kwargs = calls[0].kwargs
    assert "frame_mode" in kwargs, (
        f"the shadow factory passed no `frame_mode` KEYWORD (kwargs={sorted(kwargs)}); R1.6 is "
        f"unsatisfied, or the argument moved to a positional and every assertion here reads nothing"
    )
    observed = kwargs["frame_mode"]
    assert observed == glue_session_runner.FRAME_MODE_SYNTHETIC, (
        f"VERIFICATION_FRAME_MODE={configured!r} with source_data_uri={source_data_uri!r} produced a "
        f"shadow Glue runner with frame_mode={observed!r} — R1.6 pins the shadow half to "
        f"{glue_session_runner.FRAME_MODE_SYNTHETIC!r} REGARDLESS of the configured mode, because the "
        f"pandas half has no source read at all and a real sample against a generated frame makes every "
        f"data-quality dimension disagree for a reason that is not the executor"
    )
    assert observed == "synthetic"
    assert observed != glue_session_runner.FRAME_MODE_SAMPLE
    return observed


def _resolve(frame_mode, source_data_uri):
    """Hand *frame_mode* and *source_data_uri* to the REAL classifier.

    A real :class:`GlueSessionTestRunner` over a client that cannot be used, so
    ``resolve_frame_source`` runs its own code and R1.5's "no AWS API" is
    enforced at the seam rather than inspected afterwards. Only the
    :class:`FrameSource` is returned: going further would reach ``CreateSession``.
    """
    client = RaisingGlueClient()
    with patch("glue_session_runner.boto3.client", return_value=client):
        runner = glue_session_runner.GlueSessionTestRunner(
            REGION,
            role_arn=ROLE_ARN,
            frame_mode=frame_mode,
            source_data_uri=source_data_uri,
        )
    # The vacuity guard, not decoration: a `None` client would satisfy every
    # AWS-free claim for the wrong reason.
    assert runner.client is client, (
        "the runner fell back to `self.client = None`, so nothing here measures anything — "
        "`RaisingGlueClient` must raise on use, never on construction"
    )
    return runner.resolve_frame_source()


def _assert_the_vocabulary_still_matches_the_module():
    """Guard the generators against the runner's vocabulary moving underneath them."""
    assert set(_VOCABULARY) == {
        glue_session_runner.FRAME_MODE_SYNTHETIC,
        glue_session_runner.FRAME_MODE_SAMPLE,
    }, (
        f"this file draws configured values around {sorted(_VOCABULARY)} but the runner's pair is "
        f"{sorted({glue_session_runner.FRAME_MODE_SYNTHETIC, glue_session_runner.FRAME_MODE_SAMPLE})}"
    )


# ══════════════════════════════════════════════════════════════════════
# The controls
# ══════════════════════════════════════════════════════════════════════


class TestTheHarnessIsNotFailingOpen:
    """Four controls. Without them every property in this file can pass empty."""

    def test_the_glue_api_guard_is_armed(self):
        """Measure 3, provoked on purpose through the real ``_make_api_call`` seam.

        Without this, "no session was created" would rest on the two construction
        patches alone, and a ``monkeypatch`` that did not take would be invisible.
        """
        with pytest.raises(GlueApiGuardViolation):
            botocore.client.BaseClient._make_api_call(MagicMock(), "CreateSession", {})

    def test_the_vocabulary_the_generators_assume_is_the_runners(self):
        _assert_the_vocabulary_still_matches_the_module()

    @pytest.mark.parametrize("source_data_uri", SAMPLE_CAPABLE_URIS)
    def test_the_sample_corpus_would_really_reach_a_sample(self, source_data_uri):
        """The non-vacuity control for the "no Sample_Frame" half.

        Every URI the property draws is shown to resolve to ``is_sample`` **True**
        under an explicit ``sample`` mode. Without this, ``is_sample`` False on
        the shadow path could mean "the pin worked" or "that URI was never
        sampleable", and the property would not distinguish them.
        """
        frame_source = _resolve(glue_session_runner.FRAME_MODE_SAMPLE, source_data_uri)
        assert frame_source.is_sample is True, (
            f"{source_data_uri!r} does not reach a sample even under an explicit "
            f"{glue_session_runner.FRAME_MODE_SAMPLE!r} mode, so it cannot serve as evidence that the "
            f"shadow pin is what prevented one"
        )
        assert frame_source.read_format is not None

    def test_the_checker_rejects_a_construction_that_honoured_the_configuration(self):
        """The assertion is known to have teeth.

        :func:`_assert_pinned_to_synthetic` is provoked with the construction a
        regression would produce — the configured ``sample`` forwarded to the
        runner — and is required to fail. The wrong construction is built **by
        hand here**; nothing in this file asks ``record_shadow_verification`` to
        pass ``frame_mode`` through its ``runner_factory`` seam, which do-NOT 22
        forbids.
        """
        wrong = MagicMock()
        wrong(region=REGION, role_arn=ROLE_ARN, source_data_uri=None, frame_mode="sample")
        with pytest.raises(AssertionError):
            _assert_pinned_to_synthetic(wrong, configured="sample", source_data_uri=None)

        # And the positional-refactor guard, for the same reason.
        positional = MagicMock()
        positional(REGION, ROLE_ARN, None, "synthetic")
        with pytest.raises(AssertionError):
            _assert_pinned_to_synthetic(positional, configured="sample", source_data_uri=None)


# ══════════════════════════════════════════════════════════════════════
# Property 3 — the shadow half is always synthetic (R1.6)
# ══════════════════════════════════════════════════════════════════════


class TestTheShadowHalfIsAlwaysSynthetic:
    """**Validates: Requirements 1.6**"""

    @_over_the_discriminating_configurations
    @given(configured=_arbitrary_configured, source_data_uri=_any_source_uri)
    @_SETTINGS
    def test_any_configured_frame_mode_still_pins_the_shadow_half(self, configured, source_data_uri):
        """The property, over the configuration's whole domain.

        The quantifier is what this adds to task 10.1's example: the value is
        applied to the environment the handler reads, the drive goes through
        ``record_shadow_verification`` with the production factory, and the
        recorded ``frame_mode`` is synthetic for every draw — including
        ``"sample"``, which is what both stacks deploy.
        """
        status, constructor, writer = _shadow_run(configured, source_data_uri)
        _assert_the_shadow_half_actually_ran(status, writer)
        event(f"configured: {'recognised' if configured in _VOCABULARY else 'other'}")
        event(f"uri: {'sample-capable' if source_data_uri in SAMPLE_CAPABLE_URIS else 'not sample-capable'}")
        _assert_pinned_to_synthetic(constructor, configured=configured, source_data_uri=source_data_uri)

    @_over_the_discriminating_configurations
    @given(configured=_arbitrary_configured, source_data_uri=_sample_capable)
    @_SETTINGS
    def test_no_shadow_run_reaches_a_sample_frame(self, configured, source_data_uri):
        """The second half of the property, composed through the real classifier.

        The mode fed to ``resolve_frame_source`` is the one read back out of the
        recorded construction, not a transcription — so "the factory pins
        synthetic" and "that mode reaches no Sample_Frame" are one chain of
        observations rather than two independent claims about the same string.

        The URI is drawn from the corpus the control above proves *would* reach a
        sample, so ``is_sample`` False is attributable to the pin.

        ``requested_mode`` is asserted too, and it is the sharper of the two: it
        is what says a sample was never even **asked for** on this path, rather
        than asked for and refused. A refusal would carry an
        ``unavailable_reason`` and would mean the URI, not the pin, closed the
        door.
        """
        status, constructor, writer = _shadow_run(configured, source_data_uri)
        _assert_the_shadow_half_actually_ran(status, writer)
        observed = _assert_pinned_to_synthetic(constructor, configured=configured, source_data_uri=source_data_uri)

        frame_source = _resolve(observed, source_data_uri)
        assert frame_source.is_sample is False, (
            f"a shadow run with VERIFICATION_FRAME_MODE={configured!r} reached a Sample_Frame over "
            f"{source_data_uri!r} — R1.6 makes a sample reachable only under the non-shadow `glue` executor"
        )
        assert frame_source.mode == glue_session_runner.FRAME_MODE_SYNTHETIC
        assert frame_source.sample_requested is False, (
            f"the shadow path asked for a sample (requested_mode={frame_source.requested_mode!r}); under "
            f"R1.6 it must never ask, so that its frame is synthetic by decision rather than by refusal"
        )
        assert frame_source.unavailable_reason is None, (
            f"unavailable_reason={frame_source.unavailable_reason!r} — a shadow run builds a synthetic "
            f"frame because it never requested a sample, not because one was unavailable (C9)"
        )

    @given(configured=_arbitrary_configured, source_data_uri=_any_source_uri)
    @_SETTINGS
    def test_the_pin_is_the_same_whatever_the_uri_is(self, configured, source_data_uri):
        """The frame mode the shadow half gets does not vary with the URI either.

        Worth its own arm: a factory that read the URI — "pin synthetic only when
        the source could not be sampled anyway" — would satisfy neither R1.6 nor
        the reason for it, and would pass the first property on every
        non-sample-capable draw. Asserted as a constant rather than a comparison
        between two drives, because the constant is what R1.6 names.
        """
        status, constructor, writer = _shadow_run(configured, source_data_uri)
        _assert_the_shadow_half_actually_ran(status, writer)
        observed = _assert_pinned_to_synthetic(constructor, configured=configured, source_data_uri=source_data_uri)
        assert observed == glue_session_runner.FRAME_MODE_SYNTHETIC

    @given(configured=_arbitrary_configured, source_data_uri=_any_source_uri)
    @_SETTINGS
    def test_the_uri_still_reaches_the_runner_unchanged(self, configured, source_data_uri):
        """The pin narrows the frame mode and nothing else.

        ``source_data_uri`` remains a constructor argument (p42 R9.6, C6) and the
        shadow half forwards the run's own value. Asserted here because "pin the
        frame mode" has an over-reaching neighbour — dropping the URI as well,
        since a synthetic frame does not read it — and that would silently change
        what a later spec's provenance can report.
        """
        status, constructor, writer = _shadow_run(configured, source_data_uri)
        _assert_the_shadow_half_actually_ran(status, writer)
        kwargs = constructor.call_args_list[0].kwargs
        assert kwargs["source_data_uri"] == source_data_uri
        assert kwargs["role_arn"] == ROLE_ARN


# ══════════════════════════════════════════════════════════════════════
# do-NOT 22 — the pin is in the factory, not at the call site
# ══════════════════════════════════════════════════════════════════════


class TestThePinLivesInTheFactoryNotTheCallSite:
    """**Validates: Requirements 1.6**

    The complement of the properties above, and a separate claim rather than a
    restatement: those say the *default* factory pins synthetic; this says the
    *call site* does not decide. Both are needed — moving the pin to
    ``record_shadow_verification``'s ``factory(...)`` call would keep every
    assertion above passing while putting the mode somewhere an injected factory
    can be handed ``sample`` (do-NOT 22, design §4.2).
    """

    @_over_the_discriminating_configurations
    @given(configured=_arbitrary_configured, source_data_uri=_any_source_uri)
    @_SETTINGS
    def test_an_injected_factory_is_never_handed_a_frame_mode(self, configured, source_data_uri):
        """The seam's kwargs are exactly the three it has always taken."""
        seen = {}

        def recording_factory(**kwargs):
            seen.update(kwargs)
            runner = MagicMock()
            runner.run_test.return_value = dict(GLUE_RESULT)
            return runner

        status, constructor, writer = _shadow_run(configured, source_data_uri, runner_factory=recording_factory)
        _assert_the_shadow_half_actually_ran(status, writer)
        assert constructor.call_args_list == [], (
            "an injected factory was supplied, so the production one must not have constructed anything"
        )
        assert set(seen) == {"region", "role_arn", "source_data_uri"}, (
            f"the runner_factory seam was called with {sorted(seen)} — do-NOT 22 keeps `frame_mode` OUT "
            f"of this call, because the pin belongs inside the default factory where an injected one "
            f"cannot be handed a sample"
        )
        assert seen["source_data_uri"] == source_data_uri
        assert seen["role_arn"] == ROLE_ARN
