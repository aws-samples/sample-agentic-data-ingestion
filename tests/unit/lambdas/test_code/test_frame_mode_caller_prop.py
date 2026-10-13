# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Feature: p44-verify-against-real-source-data, Property 2: The configured value
# reaches the runner unnormalised, per run
"""Property 2 — the configured value reaches the runner unnormalised, per run.

Spec ``p44-verify-against-real-source-data`` task 9.3, design §4.1.

    *For any* two frame-mode environment values applied to two consecutive
    handler invocations in one warm process, each invocation constructs the
    runner with a ``frame_mode`` byte-equal to that invocation's own configured
    value — no normalisation, no module-level cache — and any value outside the
    recognised pair produces one WARNING naming the value verbatim.

**Validates: Requirements 1.1, 1.3, 1.4**

TWO INVOCATIONS, AND THAT IS THE WHOLE POINT OF THE HARNESS
-----------------------------------------------------------
R1.3 is not "the value is read"; it is "the value is read **at the point of
construction** rather than at import, so a warm container reflects a
configuration change on its next run". A single-invocation test cannot see the
difference — a module-level ``FRAME_MODE = os.environ.get(...)`` captured once at
import would satisfy it perfectly. So every property here drives
:func:`lambda_function.lambda_handler` **twice in one process**, against a
``GlueSessionTestRunner`` stub that persists across both, and reads
``call_args_list`` rather than ``call_args``.
:meth:`TestTheValueIsPerInvocation.test_two_different_values_produce_two_different_kwargs`
is the discriminating assertion: under a module-level read the second invocation
would report the first one's value, so the two recorded kwargs would be equal
where the two configured values differ.

That harness is inherited rather than invented. ``_warm_process`` is
``test_report_names_the_running_executor.py``'s ``_run_handler`` collaborator
stack, restructured so the patches are entered **once** and the handler is
invoked repeatedly inside them — which is what makes "one warm process" a fact
about the test rather than a claim in its docstring.

WHICH LAYER OWNS THE WARNING, AND WHY THIS FILE ASSERTS ON BOTH
---------------------------------------------------------------
The property has two halves living in two modules, and conflating them would
assert the defect do-NOT 21 forbids:

* **The caller** (``lambda_function.py:3083-3088``) passes the value **raw** —
  no ``.strip()``, no ``.lower()``, no membership check — and therefore emits **no
  frame-mode diagnostic of its own**. A WARNING here would mean a second
  classifier for this vocabulary in a second module, which is the
  second-inference-path defect, not a safeguard.
* **The classifier** (``glue_session_runner.resolve_frame_source``, ``:1099-1110``)
  is the single place that recognises the pair, and it is what names an
  unrecognised value at WARNING with ``%r``.

:class:`TestTheWarningBelongsToTheClassifierNotTheCaller` asserts both
directions, and it **composes** them rather than assuming they agree: the value
handed to the classifier is the one read back out of the caller's recorded
kwargs, so "passed unnormalised" and "named verbatim" are one chain of
observations instead of two independent claims about the same string.

``LGR-ad78946-1`` (**template / ratified**) is the governing record for the
no-normalisation half. It rejected case-insensitive matching and ``.strip()`` for
the sibling ``VERIFICATION_EXECUTOR`` in these words: "normalising removes the
only signal that the deployed value is not the intended one, so a near-miss
takes the unrecognised arm and is named in a WARNING". The argument transfers
unchanged, which is why the near-miss families below are adversarial towards a
*future* normalisation rather than towards the current code: adding either call
at the read site breaks a test that explains why it must not.

``""`` IS NOT AN UNRECOGNISED MODE, AND THE DISTINCTION IS DELIBERATE
---------------------------------------------------------------------
This is the one near-miss the task names that behaves unlike the other five, so
it is stated before the generators. The read is ``os.environ.get(ENV) or
DEFAULT`` and **not** ``os.environ.get(ENV, DEFAULT)``, so an empty value is
"unset" rather than a value outside the pair. The two-argument form would pass
``""`` through to the classifier, which would fall through to synthetic anyway —
but *via the unrecognised arm*, putting a WARNING naming ``''`` in every run of
every deployment that has not set the variable. That line is what trains a reader
to ignore the one case the WARNING exists for.

So ``""`` and an absent variable are asserted to be **indistinguishable at the
runner**, and to warn **nothing** — separately from the five genuine near-misses,
rather than flattened in with them. ``" "`` (a single space) is drawn alongside
them precisely because it is the nearest thing to ``""`` that *is* unrecognised:
it is truthy, so it survives the ``or``, reaches the classifier and earns its
WARNING. A ``.strip()`` at the read site would collapse that pair, and
:meth:`TestTheEmptyValueIsUnsetNotUnrecognised.test_a_single_space_is_not_the_empty_value`
is where it would fail.

NO GLUE SESSION IS CREATED ANYWHERE IN THIS FILE, AND NONE MAY BE
-----------------------------------------------------------------
do-NOT 1 — interactive sessions bill, and task 5.1's subagent reached a real
``CreateSession`` from an in-process exercise, so this is measured rather than
asserted in prose. Three measures:

1. **The handler drive never touches the real runner at all.** A stub module is
   registered on ``sys.modules`` and the ``glue`` branch's function-local
   ``from glue_session_runner import GlueSessionTestRunner`` picks it up, so the
   constructor is a ``MagicMock``: no boto3 client, no session, and
   ``run_test`` returns a canned contract dict. There is no code path from a
   drive to AWS.
2. **The classifier half constructs a real runner, with
   ``glue_session_runner.boto3.client`` patched**, and calls only
   :meth:`resolve_frame_source` — which R1.5 requires to reach its decision
   without any AWS API, and which returns on its first branch for every value
   outside the pair.
3. **``botocore``'s own call seam is guarded** by :func:`no_glue_api_calls`,
   copied from ``test_sample_read_failure_prop.py`` (task 6.2). It raises from
   ``_make_api_call`` for the six session operations, so a client this file's
   patches cannot see — one built at import, or through
   ``boto3.Session().client``, or because a patch target moved — hits the guard
   instead of AWS. :class:`BaseException` on purpose: ``run_test``'s third clause
   is ``except Exception`` and would swallow a plain one, route it to
   ``create_session_failure_results``, and let the test pass while a real API
   call had been attempted.
   :meth:`TestTheHarnessIsNotFailingOpen.test_the_glue_api_guard_is_armed`
   provokes it on purpose, so measure 3 is itself measured.

``make test-e2e`` and the two ``tests/e2e/test_glue_*.py`` files are forbidden
for this spec, and ``--override-ini=addopts=`` is never passed by hand.

THE NON-VACUITY ARGUMENT
------------------------
"The recorded ``frame_mode`` equals the configured value" is vacuously true if
the runner was never constructed, or if ``frame_mode`` never reached the kwargs.
Both are guarded rather than hoped for:

* :func:`_frame_modes_from` asserts one construction per invocation and that
  ``frame_mode`` is present **as a keyword**, so a positional-argument refactor
  fails here instead of silently emptying every assertion below.
* :class:`TestTheHarnessIsNotFailingOpen` carries the three controls — the log
  capture collects a known WARNING, the Glue API guard fires, and the recognised
  pair genuinely arrives unchanged (so "byte-equal" is not being satisfied by a
  runner that receives nothing at all).

WHAT THIS FILE DOES NOT DUPLICATE
---------------------------------
* ``test_frame_mode_resolution_prop.py`` (task 9.2) owns Property 1 — the
  classifier's totality and the ``s3://`` gate. Nothing here asserts a
  ``FrameSource``'s mode, URI or format beyond the WARNING the unrecognised arm
  emits on its way past.
* ``test_sample_frame_dispatch.py`` and ``test_s3_scheme_gate_prop.py`` (p42)
  own the gate and the dispatch by example and by property.
* ``test_verification_executor_selection.py`` owns the runner dispatch and the
  source-text pins on the construction site, including
  ``frame_mode=os.environ.get(...)``. This file asserts the **behaviour** that
  those pins stand in for, per ``LGR-b46a5bb-4``'s posture that a fence is
  better asserted on behaviour than on substring-matched source.
* ``test_executor_selection_totality_prop.py`` owns the *executor* vocabulary's
  near-miss families. The two whitespace alphabets are deliberately the same
  shape, because ``str.strip()`` is the same hazard in both places.
* Task 10.2 owns ``_default_shadow_glue_runner``'s synthetic pin (R1.6). Nothing
  here drives the shadow branch.

Ledger notes — including where the ledger is silent
---------------------------------------------------
* ``LGR-ad78946-1`` (**template / ratified**, ``NOTE: BROAD`` over 60 files,
  fetched in full) — the no-normalisation fence, quoted above. Its body names
  the executor value directly, so it is cited for its reasoning rather than for
  the file match.
* ``LGR-5108dd4-1`` (**template / ratified**) — the glue-first reversal, and it
  bears on this file twice. It restates that "*Normalising near-misses* stays
  rejected under ``LGR-ad78946-1`` — exact-equality membership is unchanged, no
  ``.strip()`` and no ``.lower()``", and it records the WARNING's ``%r``
  formatting as **load-bearing**, which is why the assertions below check
  ``repr(value)`` and not ``str(value)``. It is also why the drives here pin
  ``VERIFICATION_EXECUTOR`` to an exact ``"glue"``: under the reversal, any other
  spelling would resolve to ``glue`` anyway *and* add an executor WARNING to
  every captured log, which would contaminate the frame-mode warning counts.
* ``LGR-ad78946-5`` (**template / ratified**) — a ``glue`` request with no role
  ARN downgrades to ``pandas`` at ERROR. That is why every drive supplies
  ``VERIFICATION_SESSION_ROLE_ARN``: without it the ``glue`` branch is never
  entered and the runner is never constructed, which would make this whole file
  vacuous rather than failing.
* ``LGR-b46a5bb-4`` (**template**, concept ``testing``) — substring matching was
  rejected for testing an invariant because "it forbids documenting the fence",
  and it failed against a docstring that named the antipattern on purpose. This
  file's own docstring names ``.strip()`` and ``.lower()`` repeatedly, so the
  no-normalisation claim is asserted behaviourally.
* ``LGR-3e45e65-1`` (**template**, concept ``testing``) — the unit layer pins
  ``AWS_REGION`` in ``tests/unit/conftest.py`` by assignment, so the
  ``setdefault`` calls below are consistent no-ops, kept for module-import parity
  with every sibling in this directory.
* ``why`` on ``lambda_function.py`` returns four commits and **no record naming
  the frame-mode read**: task 9.1 is not committed to main, so the ledger holds
  nothing about it. Stated as a gap, not as an all-clear.
* ``decisions(concept="deployment", include_backfilled=True)`` returns three
  records, none about this flag (``LGR-b46a5bb-3`` is the Foundation-first order
  R7.5 rests on). ``decisions(concept="api")`` returns twelve, none about the
  read site. ``decisions(concept="testing")`` returns six, of which the two above
  bear on this file's *form* rather than its subject. All three areas are thin
  here rather than settled.

CodeGraph, for the symbols this reads
-------------------------------------
* ``VERIFICATION_FRAME_MODE_ENV`` (``lambda_function.py:454``) — 1 caller,
  ``lambda_handler``; ⚠️ no covering tests.
* ``FRAME_MODE_SYNTHETIC_DEFAULT`` (``:474``) — 1 caller, ``lambda_handler``;
  ⚠️ no covering tests.
* ``resolve_effective_executor`` (``:617``) — 1 caller, ``lambda_handler``;
  ⚠️ no covering tests.
* ``GlueSessionTestRunner`` (``glue_session_runner.py:535``) — reported as "43
  callers in ``lambda_function.py``"; the production figure is **2**
  (``:1007`` in ``_default_shadow_glue_runner``, ``:3083`` in the ``glue``
  branch), the rest being test sites the graph attributes to the module. 18
  covering test files.
* ``resolve_frame_source`` (``:1066``) — reported as "20 callers in
  ``glue_session_runner.py``"; the production figure is **1**
  (``run_guarded_session``). 8 covering test files.

The three ⚠️ flags on ``lambda/**`` are the documented false-negative shape —
``sys.path`` manipulation plus bare-name imports the symbol graph does not
follow. This file supplies the first coverage for the two frame-mode constants,
which is what the flags are honestly reporting: nothing named them before.
``glue_session_runner`` imports nothing from ``shared/``, and the handler's
``shared/`` imports are stubbed here exactly as in every sibling, so the
shared-layer blast radius of this task is nil.
"""

import json
import logging
import os
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import botocore.client
import pytest
from hypothesis import HealthCheck, assume, event, example, given, settings
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

# Stub the heavy / AWS-touching imports so the handler module loads in isolation.
# The same set every sibling in this directory uses. `glue_session_runner` is
# deliberately NOT stubbed at import: the drives below register a stub per
# `_warm_process`, and the classifier half needs the REAL module.
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
    sys.modules["lambda_function"] = _mod
    yield


ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

_SETTINGS = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# ══════════════════════════════════════════════════════════════════════
# do-NOT 1, measured rather than assumed
# ══════════════════════════════════════════════════════════════════════


class GlueApiGuardViolation(BaseException):
    """A test in this file reached a real Glue API call. Stub the client."""


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
    Copied from ``test_sample_read_failure_prop.py`` (task 6.2), which is the
    reusable form, rather than reinvented.
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
# The vocabulary, and the near-miss families built around it
# ══════════════════════════════════════════════════════════════════════

#: The recognised pair, spelled out rather than read off either module, then
#: checked against both by :func:`_assert_vocabulary_matches_both_modules`.
#: Deriving the generators from the implementation would make the near-miss
#: families track a renamed member silently, and a generator that follows the
#: code it tests cannot detect the code moving (``LGR-28b4d75-5``'s posture).
_VOCABULARY = ("synthetic", "sample")

#: ``str.strip()`` removes every one of these. ``\u00a0`` (no-break space) and
#: ``\u2009`` (thin space) are here for that reason: they are what a value pasted
#: out of a rendered document or a wiki table carries, and a normalisation that
#: looked like it only touched ASCII would still strip them.
_WHITESPACE = ("\u0020", "\t", "\n", "\r", "\x0b", "\x0c", "\u00a0", "\u2009", "  ", " \t")

#: The six draws task 9.3 names, first and in its order, then the rest grouped by
#: the mistake that produces them.
#:
#: ``""`` is in the list because the task requires it, and it is the ONE entry
#: that is not an unrecognised mode — the ``or`` makes it "unset". Every property
#: below that requires an unrecognised value therefore draws from
#: :data:`_UNRECOGNISED_MODES`, which excludes it, and the empty case is asserted
#: on its own in :class:`TestTheEmptyValueIsUnsetNotUnrecognised`.
_NAMED_NEAR_MISSES = (
    # The six the task enumerates, to attack the normalisation half.
    "Sample",
    "sample ",
    "SAMPLE",
    "samples",
    "smaple",
    "",
    # The nearest thing to `""` that IS unrecognised: truthy, so it survives the
    # `or` and reaches the classifier. A `.strip()` would collapse the pair.
    " ",
    # The other member's near-misses, so the families are not all about `sample`.
    "Synthetic",
    "SYNTHETIC",
    " synthetic",
    "synthetic ",
    "synthetics",
    "syntehtic",
    # The USE_AGENTCORE_RUNTIME vocabulary applied to the wrong flag.
    "true",
    "false",
    "1",
    "0",
    # The shapes a templating layer or a shell emits for "unset".
    "none",
    "None",
    "null",
    "undefined",
    # Plausible spellings from an adjacent vocabulary.
    "real",
    "generated",
    "source",
    "s3",
    "glue",
    "pandas",
    # Quoted and list-ish: what a JSON-valued env block or a mis-escaped CDK
    # literal delivers. The value arrives WITH its quotes.
    '"sample"',
    "'sample'",
    "['sample']",
    "sample,synthetic",
    "sample|synthetic",
    "sample synthetic",
)

#: Characters safe to put in ``os.environ``. NUL is excluded because
#: ``os.environ[k] = v`` raises ``ValueError: embedded null byte``, and surrogates
#: because they cannot be encoded — a draw of either would fail this file's
#: harness rather than its subject.
#:
#: The single-character members of :data:`_WHITESPACE` are added back after the
#: ``Cc`` exclusion removes them, so the free-text family can still produce the
#: shapes a ``.strip()`` would rescue. The two multi-character members are
#: reached through :func:`_whitespace_near_misses` instead.
_ENV_SAFE_CHARACTERS = st.characters(
    codec="utf-8",
    exclude_categories=("Cs", "Cc"),
    include_characters=tuple(char for char in _WHITESPACE if len(char) == 1),
)


@st.composite
def _casing_near_misses(draw):
    """A member with an arbitrary subset of its letters upper-cased.

    ``assume`` discards the all-lower draw, which reproduces the member itself
    and is therefore a *recognised* value rather than a near-miss.
    """
    member = draw(st.sampled_from(_VOCABULARY))
    flips = draw(st.lists(st.booleans(), min_size=len(member), max_size=len(member)))
    candidate = "".join(char.upper() if flip else char for char, flip in zip(member, flips, strict=True))
    assume(candidate not in _VOCABULARY)
    return candidate


@st.composite
def _whitespace_near_misses(draw):
    """A member with whitespace added on one or both sides.

    ``assume`` discards the empty/empty draw for the same reason as above. These
    are the draws a ``.strip()`` at the read site would rescue, which is exactly
    why they must stay refused.
    """
    member = draw(st.sampled_from(_VOCABULARY))
    leading = draw(st.sampled_from(("", *_WHITESPACE)))
    trailing = draw(st.sampled_from(("", *_WHITESPACE)))
    candidate = f"{leading}{member}{trailing}"
    assume(candidate not in _VOCABULARY)
    return candidate


@st.composite
def _interior_near_misses(draw):
    """A member with a character inserted strictly inside it.

    Distinct from the whitespace family: neither trimming nor lower-casing
    recovers the member, so these stay refused under *both* normalisations
    ``LGR-ad78946-1`` rejected. They keep the refused population from being made
    entirely of shapes a ``.strip()`` would rescue — otherwise a resolver that
    normalised would look better against this file than it deserves.

    The insertion position excludes both ends deliberately: inserting whitespace
    at position 0 or at the end would reproduce a :func:`_whitespace_near_misses`
    draw, and that draw *is* recoverable by trimming, which would falsify the
    paragraph above.
    """
    member = draw(st.sampled_from(_VOCABULARY))
    position = draw(st.integers(min_value=1, max_value=len(member) - 1))
    inserted = draw(st.sampled_from(("_", "-", ".", " ", "x", "0")))
    return f"{member[:position]}{inserted}{member[position:]}"


#: Arbitrary configured values, over the declared domain ``str | None``.
#:
#: ``None`` means the variable is **unset**, which is a distinct state from
#: ``""`` and is generated as such. The recognised members are included on
#: purpose: the per-invocation properties are statements about the WHOLE domain,
#: so the strategy feeding them must draw the accepted values too.
_arbitrary_configured = st.one_of(
    st.none(),
    st.sampled_from(_VOCABULARY),
    st.sampled_from(_NAMED_NEAR_MISSES),
    st.text(alphabet=_ENV_SAFE_CHARACTERS, max_size=12),
    _casing_near_misses(),
    _whitespace_near_misses(),
    _interior_near_misses(),
)

#: Configured values that are strings, non-empty, and outside the recognised
#: pair — i.e. the values that genuinely reach the classifier's unrecognised arm.
#:
#: Never ``None`` and never ``""``: both are "unset" by the time the runner sees
#: them, so both would falsify the WARNING properties for the wrong reason.
_UNRECOGNISED_MODES = st.one_of(
    st.sampled_from([value for value in _NAMED_NEAR_MISSES if value != ""]),
    st.text(alphabet=_ENV_SAFE_CHARACTERS, min_size=1, max_size=12).filter(lambda v: v not in _VOCABULARY),
    _casing_near_misses(),
    _whitespace_near_misses(),
    _interior_near_misses(),
)


def _assert_vocabulary_matches_both_modules():
    """Guard the generators against either module moving underneath them.

    Every "outside the pair" property here is only as strong as
    :data:`_VOCABULARY` being the real pair, **in both spellings**. C13's
    convention is that ``lambda_function.FRAME_MODE_SYNTHETIC_DEFAULT`` is a
    deliberate second spelling of ``glue_session_runner.FRAME_MODE_SYNTHETIC``
    — change both or neither — so this is also the drift check between them.
    """
    assert set(_VOCABULARY) == {
        glue_session_runner.FRAME_MODE_SYNTHETIC,
        glue_session_runner.FRAME_MODE_SAMPLE,
    }, (
        f"this file generates near-misses around {sorted(_VOCABULARY)} but the runner's pair is "
        f"{sorted({glue_session_runner.FRAME_MODE_SYNTHETIC, glue_session_runner.FRAME_MODE_SAMPLE})}; "
        f"the generated populations no longer describe it"
    )
    assert _mod.FRAME_MODE_SYNTHETIC_DEFAULT == glue_session_runner.FRAME_MODE_SYNTHETIC, (
        f"the two spellings of the synthetic mode have drifted: the caller's default is "
        f"{_mod.FRAME_MODE_SYNTHETIC_DEFAULT!r} and the runner's constant is "
        f"{glue_session_runner.FRAME_MODE_SYNTHETIC!r} — C13 is change both or neither"
    )
    assert _mod.VERIFICATION_FRAME_MODE_ENV == "VERIFICATION_FRAME_MODE"


def _over_named_near_misses(test):
    """Stack one ``@example`` per unrecognised entry of :data:`_NAMED_NEAR_MISSES`.

    The task enumerates six of these as load-bearing, so their coverage is
    pinned rather than left to the seed. ``_UNRECOGNISED_MODES`` samples from the
    same tuple and reaches all of it over 200 draws, but "reaches with high
    probability" is not "always reaches". ``""`` is skipped because it is not an
    unrecognised mode — see :class:`TestTheEmptyValueIsUnsetNotUnrecognised`.
    """
    for value in reversed(_NAMED_NEAR_MISSES):
        if value == "":
            continue
        test = example(configured=value)(test)
    return test


# ══════════════════════════════════════════════════════════════════════
# The log capture
# ══════════════════════════════════════════════════════════════════════


@contextmanager
def _captured_records(logger):
    """Collect the records *logger* emits during the block.

    A per-call ``logging.Handler`` rather than ``caplog``: ``caplog`` is
    function-scoped, so under ``@given`` it is applied once for the whole
    function and accumulates across examples. This is per-example by
    construction and still goes through real record creation, so
    ``record.getMessage()`` renders exactly what a log reader sees — including
    the ``%r`` that makes ``'sample '`` legible, which ``LGR-5108dd4-1`` records
    as load-bearing.

    No level manipulation: WARNING and ERROR both exceed the Powertools
    ``Logger`` default of INFO.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collector(level=logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


def _messages_at(records: list[logging.LogRecord], level: str) -> list[str]:
    """Rendered messages at *level*, in emission order."""
    return [record.getMessage() for record in records if record.levelname == level]


# ══════════════════════════════════════════════════════════════════════
# The warm process
# ══════════════════════════════════════════════════════════════════════

_CODE_GEN_ITEM = {
    "run_id": "run-1",
    "step_name": "create_code",
    "status": "success",
    "output_data": {"code_version_id": "cv-1", "code_s3_uri": "s3://code-bucket/code.py"},
}

_PASS_VERDICT = {
    "verdict": "pass",
    "issues": [],
    "categories": {
        "Correctness": "pass",
        "Schema Compliance": "pass",
        "Error Handling": "pass",
        "AWS Best Practices": "pass",
        "Security": "pass",
        "Performance": "pass",
    },
}

#: A clean successful execution, so no drive produces an execution WARNING that
#: would contaminate the frame-mode warning counts.
_EXEC_OK = {
    "execution_successful": True,
    "executed": True,
    "exit_code": 0,
    "stderr": "",
    "error_details": None,
    "input_records": 100,
    "output_records": 100,
    "conversion_report": None,
    "data_quality_results": {
        "overall_quality_score": 98,
        "completeness_score": 100,
        "uniqueness_score": 100,
        "schema_compliance": 100,
        "duplicates": 0,
        "missing_columns": [],
    },
}


def _fake_dynamodb() -> MagicMock:
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": {"pipeline_id": "pipe-1"}}
    dynamodb = MagicMock()
    dynamodb.Table.return_value = pipelines_table
    return dynamodb


@contextmanager
def _warm_process():
    """Enter the handler's collaborator patches ONCE and yield an invoker.

    The collaborator stack is ``test_report_names_the_running_executor.py``'s
    ``_run_handler``, restructured: the patches — including the
    ``glue_session_runner`` stub whose ``GlueSessionTestRunner`` records the
    kwargs — live outside the invoker, so a second call is genuinely a second
    invocation in one warm process against one accumulating mock. That is what
    R1.3's "a warm container reflects a configuration change on its next run"
    needs in order to be observable at all.

    Yields ``(invoke, glue_runner_stub)``. ``invoke(configured)`` sets the
    frame-mode variable — or removes it, for ``None`` — and drives the handler
    once. No Glue client is constructed and no session is created: the stub
    module means the ``glue`` branch's function-local import never reaches the
    real runner.
    """
    runs_table = MagicMock()
    runs_table.get_item.return_value = {"Item": _CODE_GEN_ITEM}
    code_table = MagicMock()
    code_table.get_item.return_value = {"Item": {"PK": "CODE#cv-1", "SK": "VERSION"}}
    results_table = MagicMock()
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"print('transform')"))}

    agent = MagicMock(return_value=json.dumps(_PASS_VERDICT))
    agent.add_hook = MagicMock()

    runner = MagicMock()
    runner.run_test.return_value = _EXEC_OK

    # The `glue` branch imports `GlueSessionTestRunner` function-locally, so the
    # patch has to be on the module it imports FROM. A stub module is registered
    # rather than the real one imported: no Glue client is constructed and no
    # session is created anywhere in a drive.
    glue_module = MagicMock()
    glue_module.GlueSessionTestRunner = MagicMock(return_value=runner)

    def _invoke(configured: str | None) -> None:
        # `VERIFICATION_EXECUTOR` is pinned to an exact `"glue"` and the role ARN
        # is supplied, so the `glue` branch is entered (LGR-ad78946-5) and no
        # executor WARNING is emitted (LGR-5108dd4-1).
        env = {
            "USE_AGENTCORE_RUNTIME": "false",
            _mod.VERIFICATION_EXECUTOR_ENV: "glue",
            _mod.VERIFICATION_SESSION_ROLE_ARN_ENV: ROLE_ARN,
        }
        if configured is not None:
            env[_mod.VERIFICATION_FRAME_MODE_ENV] = configured
        with patch.dict("os.environ", env):
            if configured is None:
                # `patch.dict` adds and overrides but never removes, and it
                # restores the whole mapping on exit — so popping inside is how
                # "unset" is expressed, and it is safe.
                os.environ.pop(_mod.VERIFICATION_FRAME_MODE_ENV, None)
            _mod.lambda_handler({"run_id": "run-1", "pipeline_id": "pipe-1"}, None)

    with (
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "generated_code_table", code_table),
        patch.object(_mod, "test_results_table", results_table),
        patch.object(_mod, "s3_client", s3),
        patch.object(_mod, "dynamodb", _fake_dynamodb()),
        patch.object(_mod, "Agent", MagicMock(return_value=agent)),
        patch.object(_mod, "AgentTracePlugin", MagicMock()),
        patch.object(_mod, "CodeInterpreterTestRunner", MagicMock(return_value=runner)),
        patch.dict("sys.modules", {"glue_session_runner": glue_module}),
    ):
        yield _invoke, glue_module.GlueSessionTestRunner


def _frame_modes_from(stub: MagicMock, expected_invocations: int) -> list[str]:
    """The ``frame_mode`` kwarg of each recorded construction, in order.

    **The two anti-vacuity guards live here**, so every assertion downstream
    inherits them rather than restating them:

    1. one construction per invocation — a drive that fell to the pandas arm, or
       raised before Phase 2, would leave this short and fail here instead of
       emptying a byte-equality assertion;
    2. ``frame_mode`` present **as a keyword** — a positional-argument refactor
       would otherwise make every ``kwargs`` read return nothing and every
       property below vacuously true.
    """
    calls = stub.call_args_list
    assert len(calls) == expected_invocations, (
        f"expected {expected_invocations} runner construction(s), got {len(calls)} — the `glue` branch was "
        f"not reached on every invocation, so the property below would be vacuous"
    )
    modes = []
    for index, call in enumerate(calls):
        assert "frame_mode" in call.kwargs, (
            f"construction {index} passed no `frame_mode` KEYWORD (kwargs={sorted(call.kwargs)}); R1.1 is "
            f"unsatisfied, or the argument moved to a positional and every assertion here reads nothing"
        )
        modes.append(call.kwargs["frame_mode"])
    return modes


def _resolve_with(frame_mode: str, source_data_uri: str | None = None):
    """Hand *frame_mode* to the REAL classifier and return its records + answer.

    A real :class:`GlueSessionTestRunner` over a patched ``boto3.client``, so
    ``resolve_frame_source`` runs its own code. R1.5 requires that decision to
    touch no AWS API, and for every value outside the pair it returns on its
    first branch — but :func:`no_glue_api_calls` is the measurement of that, not
    this docstring.
    """
    with patch("glue_session_runner.boto3.client", return_value=MagicMock()):
        runner = glue_session_runner.GlueSessionTestRunner(
            REGION, role_arn=ROLE_ARN, frame_mode=frame_mode, source_data_uri=source_data_uri
        )
    with _captured_records(glue_session_runner.logger) as records:
        frame_source = runner.resolve_frame_source()
    return records, frame_source


# ══════════════════════════════════════════════════════════════════════
# The controls
# ══════════════════════════════════════════════════════════════════════


class TestTheHarnessIsNotFailingOpen:
    """Three controls. Without them every property in this file can pass empty."""

    def test_the_generated_populations_describe_both_modules(self):
        """The control for every "outside the pair" property below."""
        _assert_vocabulary_matches_both_modules()

    def test_the_glue_api_guard_is_armed(self):
        """Measure 3, provoked on purpose through the real ``_make_api_call`` seam.

        Without this, a ``monkeypatch`` that did not take would leave every "no
        session was created" claim resting on the construction-seam patches
        alone.
        """
        with pytest.raises(GlueApiGuardViolation):
            botocore.client.BaseClient._make_api_call(MagicMock(), "CreateSession", {})

    def test_the_log_capture_collects_a_known_warning(self):
        """Positive control for :func:`_captured_records`.

        Every "warns nothing" assertion here is vacuous if the capture collects
        nothing, and a broken capture would leave only the "warns once"
        assertions failing — which reads as a classifier defect rather than a
        harness one.
        """
        records, _ = _resolve_with("definitely-not-a-frame-mode")
        assert len(_messages_at(records, "WARNING")) == 1

    @pytest.mark.parametrize("configured", _VOCABULARY)
    def test_a_recognised_value_genuinely_arrives_at_the_runner(self, configured):
        """ "Byte-equal" is not satisfied by a runner that receives nothing.

        The recognised pair is the case where the expected value and the drawn
        value coincide for an uninteresting reason, so it is pinned by example
        as the floor under the properties: the plumbing works at all.
        """
        with _warm_process() as (invoke, stub):
            invoke(configured)
            assert _frame_modes_from(stub, 1) == [configured]


# ══════════════════════════════════════════════════════════════════════
# The value is per-invocation — R1.1, R1.3
# ══════════════════════════════════════════════════════════════════════


class TestTheValueIsPerInvocation:
    """**Validates: Requirements 1.1, 1.3**"""

    @given(first=_arbitrary_configured, second=_arbitrary_configured)
    @_SETTINGS
    def test_each_invocation_gets_its_own_configured_value(self, first, second):
        """R1.1 + R1.3 — the property, in its two-invocation form.

        Each of the two consecutive invocations must report **its own** value.
        The assertion is split by truthiness because only one half is
        derivation-free, and saying so is the point:

        * a **truthy** configured value is compared byte-for-byte against the
          drawn input, so nothing is derived from the code under test — this is
          the no-normalisation half, and a ``.strip()`` or ``.lower()`` at the
          read site fails it;
        * a **falsy** one (unset, or ``""``) is compared against the synthetic
          spelling, which is the ``or``'s documented behaviour rather than an
          incidental one — see
          :class:`TestTheEmptyValueIsUnsetNotUnrecognised`.
        """
        with _warm_process() as (invoke, stub):
            invoke(first)
            invoke(second)
            recorded = _frame_modes_from(stub, 2)

        for index, (configured, observed) in enumerate(zip((first, second), recorded, strict=True)):
            event(f"invocation {index}: {'recognised' if configured in _VOCABULARY else 'other'}")
            if configured:
                assert observed == configured, (
                    f"invocation {index} configured {configured!r} but the runner received {observed!r} — "
                    f"the value was normalised or cached, and LGR-ad78946-1 forbids both"
                )
            else:
                assert observed == _mod.FRAME_MODE_SYNTHETIC_DEFAULT
                assert observed == "synthetic"

    @given(first=_arbitrary_configured, second=_arbitrary_configured)
    @_SETTINGS
    def test_two_different_values_produce_two_different_kwargs(self, first, second):
        """The discriminating assertion against a module-level read.

        This is the one test in the file a module-level
        ``FRAME_MODE = os.environ.get(...)`` would fail. Under such a read the
        second invocation reports the first's value, so the two recorded kwargs
        would be **equal** wherever the two configured values differ — and the
        single-invocation property above would still pass.

        The antecedent is on the *effective* values rather than the raw draws,
        because ``None`` and ``""`` are two spellings of one state and must not
        be asserted to differ.
        """
        with _warm_process() as (invoke, stub):
            invoke(first)
            invoke(second)
            recorded = _frame_modes_from(stub, 2)

        effective = tuple(value or _mod.FRAME_MODE_SYNTHETIC_DEFAULT for value in (first, second))
        if effective[0] != effective[1]:
            event("the two invocations differ")
            assert recorded[0] != recorded[1], (
                f"configured {first!r} then {second!r}, but both invocations received {recorded[0]!r} — "
                f"the value is cached across invocations, which R1.3 forbids"
            )
        else:
            assert recorded[0] == recorded[1]

    @given(configured=_arbitrary_configured, repeats=st.integers(min_value=2, max_value=4))
    @_SETTINGS
    def test_repeating_one_value_is_stable_across_invocations(self, configured, repeats):
        """The converse: an unchanged configuration does not drift.

        Worth asserting separately because a read that was per-invocation but
        *stateful* — appending, or mutating a cached string — would satisfy the
        two-different-values property and fail this one.
        """
        with _warm_process() as (invoke, stub):
            for _ in range(repeats):
                invoke(configured)
            recorded = _frame_modes_from(stub, repeats)

        assert len(set(recorded)) == 1, f"{configured!r} produced {sorted(set(recorded))} across {repeats} runs"

    @_over_named_near_misses
    @given(configured=_UNRECOGNISED_MODES)
    @_SETTINGS
    def test_a_near_miss_reaches_the_runner_verbatim(self, configured):
        """R1.4's precondition, and the whole of the no-normalisation half.

        A value outside the pair can only be *named* in a WARNING if it survives
        the read intact. ``LGR-ad78946-1``: "normalising removes the only signal
        that the deployed value is not the intended one, so a near-miss takes the
        unrecognised arm and is named in a WARNING".

        The second assertion is what makes this a fence rather than a
        restatement: the near-miss is asserted to be genuinely one, so a reader
        can see that some normalisation *would* have changed the answer.
        """
        with _warm_process() as (invoke, stub):
            invoke(configured)
            observed = _frame_modes_from(stub, 1)[0]

        assert observed == configured, f"{configured!r} arrived as {observed!r}"
        assert observed not in _VOCABULARY, (
            f"{configured!r} was expected to be outside {sorted(_VOCABULARY)} — the generator drew a "
            f"recognised value, so this property is not testing what it names"
        )


# ══════════════════════════════════════════════════════════════════════
# `""` is unset, not unrecognised
# ══════════════════════════════════════════════════════════════════════


class TestTheEmptyValueIsUnsetNotUnrecognised:
    """**Validates: Requirements 1.3, 1.4** — the ``or``, asserted as behaviour.

    Task 9.1's comment records the choice: ``or`` rather than
    ``os.environ.get(..., DEFAULT)``, so that a deployment which has not set the
    variable does not log a WARNING on **every** run. This class is that comment
    made executable, and it is kept separate from the near-miss properties
    because flattening the two would assert the opposite of what was decided.
    """

    def test_an_empty_value_and_an_absent_one_are_indistinguishable_at_the_runner(self):
        """Both spellings of "unset" reach the runner as the synthetic mode."""
        with _warm_process() as (invoke, stub):
            invoke(None)
            invoke("")
            recorded = _frame_modes_from(stub, 2)

        assert recorded == [_mod.FRAME_MODE_SYNTHETIC_DEFAULT, _mod.FRAME_MODE_SYNTHETIC_DEFAULT]
        assert recorded[0] == recorded[1]

    @pytest.mark.parametrize("configured", [None, ""])
    def test_neither_spelling_of_unset_earns_a_warning(self, configured):
        """The reason the ``or`` exists, asserted at the classifier.

        ``os.environ.get(ENV, DEFAULT)`` would pass ``""`` through, and the
        classifier would then warn on every run of every deployment that has not
        set the variable — the line that trains a reader to ignore the one case
        the WARNING is for.
        """
        with _warm_process() as (invoke, stub):
            invoke(configured)
            observed = _frame_modes_from(stub, 1)[0]

        records, frame_source = _resolve_with(observed)
        assert _messages_at(records, "WARNING") == []
        assert frame_source.mode == glue_session_runner.FRAME_MODE_SYNTHETIC
        assert frame_source.requested_mode == glue_session_runner.FRAME_MODE_SYNTHETIC

    def test_a_single_space_is_not_the_empty_value(self):
        """The pair a ``.strip()`` at the read site would collapse.

        ``" "`` is truthy, so it survives the ``or``, reaches the classifier and
        earns its WARNING — whereas ``""`` does not. Under a ``.strip()`` the two
        would be one state, and this is where that fails.
        """
        with _warm_process() as (invoke, stub):
            invoke(" ")
            invoke("")
            spaced, empty = _frame_modes_from(stub, 2)

        assert spaced == " "
        assert empty == _mod.FRAME_MODE_SYNTHETIC_DEFAULT
        assert spaced != empty

        spaced_records, _ = _resolve_with(spaced)
        empty_records, _ = _resolve_with(empty)
        assert len(_messages_at(spaced_records, "WARNING")) == 1
        assert _messages_at(empty_records, "WARNING") == []


# ══════════════════════════════════════════════════════════════════════
# The WARNING — R1.4, and which layer owns it
# ══════════════════════════════════════════════════════════════════════


class TestTheWarningBelongsToTheClassifierNotTheCaller:
    """**Validates: Requirement 1.4**

    Both directions, and composed rather than assumed: the value handed to the
    classifier is read back out of the caller's recorded kwargs, so this asserts
    one chain of observations instead of two independent claims about a string.
    """

    @_over_named_near_misses
    @given(configured=_UNRECOGNISED_MODES)
    @_SETTINGS
    def test_exactly_one_warning_names_the_value_verbatim(self, configured):
        """R1.4 — falling through to synthetic is correct, but it must be visible.

        Three separate claims, because each fails differently: exactly one record
        (not zero, not one per recognised member), the value rendered with
        ``repr`` so a trailing space or a casing slip is legible
        (``LGR-5108dd4-1`` records the ``%r`` as load-bearing), and both
        recognised values named so the reader knows what to type instead.
        """
        with _warm_process() as (invoke, stub):
            invoke(configured)
            observed = _frame_modes_from(stub, 1)[0]

        records, frame_source = _resolve_with(observed)
        warnings = _messages_at(records, "WARNING")

        assert len(warnings) == 1, f"{configured!r} produced {len(warnings)} warnings: {warnings}"
        assert repr(configured) in warnings[0], f"{configured!r} is not named in {warnings[0]!r}"
        assert repr(glue_session_runner.FRAME_MODE_SYNTHETIC) in warnings[0]
        assert repr(glue_session_runner.FRAME_MODE_SAMPLE) in warnings[0]
        # R1.4's other half: the fall-through is synthetic, and `requested_mode`
        # keeps the raw value so a later reader can still see what was asked for.
        assert frame_source.mode == glue_session_runner.FRAME_MODE_SYNTHETIC
        assert frame_source.requested_mode == configured

    @_over_named_near_misses
    @given(configured=_UNRECOGNISED_MODES)
    @_SETTINGS
    def test_the_caller_emits_no_frame_mode_diagnostic_of_its_own(self, configured):
        """do-NOT 21 — one classifier for this vocabulary, not two.

        The caller passes the value raw and says nothing about it.
        ``resolve_frame_source`` is the single place that recognises the pair; a
        WARNING emitted here as well would be a second inference path, and the
        two copies would be free to disagree about what counts as recognised.

        Asserted on the handler's own logger during a real drive rather than on
        ``lambda_function``'s source text — ``LGR-b46a5bb-4`` rejected substring
        matching for exactly this kind of fence, because it forbids documenting
        the fence, and this file's docstring names ``.strip()`` repeatedly.

        **Asserted as a DIFFERENCE against an unset baseline, not as "zero
        warnings".** This harness legitimately produces three of them — no target
        schema on the pipeline, no source schema on the state row, and the Strands
        MCP fallback — so a zero assertion would be false for reasons that have
        nothing to do with the frame mode. Equality against the baseline is both
        tighter and derivation-free: whatever the caller says, it says the same
        thing whether the variable is unset or carries a near-miss, so it is
        saying nothing *about the frame mode*. A substring filter over the
        messages would have been the fail-open version — a short drawn value can
        coincide with unrelated text.
        """
        with _warm_process() as (invoke, stub):
            with _captured_records(_mod.logger) as baseline_records:
                invoke(None)
            with _captured_records(_mod.logger) as records:
                invoke(configured)
            observed = _frame_modes_from(stub, 2)[1]

        assert observed == configured
        baseline = _messages_at(baseline_records, "WARNING")
        # Non-vacuity: the baseline is non-empty, so the equality below is a real
        # comparison rather than `[] == []`.
        assert baseline, "the baseline drive emitted no WARNING, so the equality below proves nothing"
        assert _messages_at(records, "WARNING") == baseline, (
            f"the caller's log changed when the frame mode became {configured!r}; the classifier owns "
            f"that diagnostic (do-NOT 21), and two copies of the vocabulary can disagree"
        )
        assert _messages_at(records, "ERROR") == _messages_at(baseline_records, "ERROR") == []

    @pytest.mark.parametrize("configured", _VOCABULARY)
    def test_a_recognised_value_warns_nothing_at_either_layer(self, configured):
        """The converse of R1.4, and what keeps the WARNING worth reading.

        A warning on every run trains the reader to ignore the one case the
        warning exists for.

        The caller half is again a difference against the unset baseline, for the
        reason
        :meth:`test_the_caller_emits_no_frame_mode_diagnostic_of_its_own`
        records: this harness emits three legitimate warnings that are nothing to
        do with the frame mode.
        """
        with _warm_process() as (invoke, stub):
            with _captured_records(_mod.logger) as baseline_records:
                invoke(None)
            with _captured_records(_mod.logger) as caller_records:
                invoke(configured)
            observed = _frame_modes_from(stub, 2)[1]

        assert observed == configured
        assert _messages_at(baseline_records, "WARNING")
        assert _messages_at(caller_records, "WARNING") == _messages_at(baseline_records, "WARNING")

        # `source_data_uri=None` on purpose: `"sample"` then takes the
        # not-an-s3-source fallback, which logs at INFO. That is task 9.2's
        # subject, not this file's — what matters here is that it is not a
        # WARNING, because the mode itself was recognised.
        classifier_records, _ = _resolve_with(observed)
        assert _messages_at(classifier_records, "WARNING") == []
