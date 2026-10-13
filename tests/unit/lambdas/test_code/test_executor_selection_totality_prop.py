# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Feature: real-spark-verification, Property 1: Executor selection is total and defaults safely
"""Property 1 — executor selection is total and defaults safely.

Spec ``real-spark-verification`` task 9.2. **Validates: Requirements 2.1, 2.2,
2.3, 2.4, 11.2.**

    *For any* string value of the executor configuration, including absent and
    empty, resolution returns exactly one of the three known executors; returns
    the **glue** executor for every value outside the recognised set; and emits
    a WARNING naming the value it received for every such value.

**The fall-through was ``pandas`` and is now ``glue``.** Verification is
glue-first by the user's decision — "it should always be glue first" — so
``pandas`` and ``shadow`` are reachable only by their own exact spelling.
Every assertion below that encoded the old landing spot is **amended in place
with its reversal stated**, per ``LGR-60c4985-6`` (template / ratified), never
deleted. The two that inverted completely rather than moving —
:meth:`TestGlueIsUnreachableByAccident.test_nothing_reaches_pandas_or_shadow_by_accident`
and :meth:`TestGlueIsUnreachableByAccident.
test_an_unrecognised_value_now_does_reach_a_glue_session` — say so in their own
docstrings, because the claims they used to make are now false by decision and
hiding that would read as a regression.

Task 9.1 established the named cases by example in
``test_verification_executor_selection.py``; that file's own docstring defers the
exhaustive form here. What follows generalises over arbitrary strings and adds
the two directions an enumerated list cannot give: that **no** unlisted spelling
reaches the Glue runner, and that the two diagnostics the resolution emits are
mutually exclusive.

The near-misses are asserted to be REFUSED, not accepted
--------------------------------------------------------

This is the one thing about this file that is easy to get backwards, so it is
stated before anything else, and the glue-first reversal makes it easier to get
backwards rather than harder.

``"Glue"``, ``" glue"``, ``"GLUE"`` and ``"glue "`` are still **REFUSED** — they
are not recognised as ``glue`` — **and each still earns a WARNING**. What changed
is only where the refusal lands: they take the unrecognised arm, and that arm now
returns ``glue``. So the *observable* result of ``" glue"`` coincides with the
result of ``glue``, and that coincidence is NOT normalisation.

The test that proves the difference is ``"shadow "``: under normalisation it
would resolve to ``shadow``; under the refusal-plus-glue-fall-through it resolves
to ``glue``. :meth:`TestUnrecognisedValuesDefaultToGlue.
test_a_whitespace_near_miss_is_refused` asserts exactly that, over all three
members. The fence is intact; only the landing spot moved.

Design §4.2 follows the ``USE_AGENTCORE_RUNTIME`` precedent already in
``lambda_function.py`` — ``os.environ.get(...) == "true"``, a plain compare
against an explicit string — and the 9.1 docstring records case-insensitive
matching and ``.strip()`` as explicitly **rejected**: normalising removes the
only signal that the configuration is wrong. A tolerant resolver accepts
``" glue "`` silently, and the operator who typed it never learns that the
value in their stack is not the value they meant.

So the generators here are adversarial towards a *future* normalisation rather
than towards the current code. :func:`_casing_near_misses` and
:func:`_whitespace_near_misses` exist specifically so that adding ``.strip()``
or ``.lower()`` to :func:`resolve_verification_executor` breaks a test that
explains why it must not. The whitespace alphabet includes ``\\u00a0`` and
``\\u2009`` because ``str.strip()`` removes both — a normalisation that looked
like it only touched ASCII spaces would still be caught.

Requirement 11.2, stated as a property rather than as a constant
---------------------------------------------------------------

R11.2 kept the *deployed* default at ``pandas`` while the concurrent-session
measurement was absent, and that measurement is still absent: the task 1.3 probe
was written and never run (running it creates billable sessions).

**Both halves of R11.2 have now been reversed, in two separate steps.** The
deployed value went to ``glue`` first (``LGR-87b8538-1``), which that record
tolerated partly because "R2.3's fall-through to pandas is a second layer,
unchanged". This change removes that second layer: the *code* fall-through is now
``glue`` too. R11.2's precondition is still unmet, so what stands is a widened
accepted deviation rather than a satisfaction of the requirement.

The property this class used to assert — no input but the exact string ``glue``
resolves to ``glue`` — is therefore **false by decision** and is amended rather
than deleted. What survives is the half that still holds and is still worth
having: nothing reaches ``pandas`` or ``shadow`` except its own exact spelling,
so the two non-default executors remain unreachable by accident, and every
unrecognised value is named in exactly one WARNING. See
:class:`TestGlueIsUnreachableByAccident` — kept under its original name, with its
docstring recording what the reversal cost.

How the log is read, and why not ``caplog``
-------------------------------------------

``caplog`` is function-scoped, and a function-scoped fixture inside a ``@given``
test is applied once for the whole function rather than once per example — so
records accumulate across examples and hypothesis raises
``HealthCheck.function_scoped_fixture``. Suppressing the check and clearing the
fixture per example would work; attaching a collecting ``logging.Handler`` to the
module's own logger for the duration of one call is simpler and exercises the
real record path, including the ``%r`` formatting that makes a trailing space
legible (:func:`_captured_records`).

WARNING and ERROR both exceed the Powertools ``Logger`` default of INFO, so the
capture needs no level manipulation.

**The capture has a positive control, deliberately.** ``LGR-b2f79ef-8``
(**template / ratified**) is the governing precedent for treating a log line as
behaviour: it kept a ``logger.error`` because the log was the only place a
distinction between two failure causes stayed observable. Asserting on a log
therefore has the fail-open hazard the sibling property files name — a capture
that silently collected nothing would make every "emits no warning" assertion
vacuously true and every "emits one warning" assertion the only thing failing.
:meth:`TestTheWarningNamesTheValue.test_the_capture_harness_is_not_failing_open`
is that control, and :func:`_assert_vocabulary_matches_the_module` is the
matching control for the generators.

What this file deliberately does not touch
------------------------------------------

*Only* the two resolution functions, which are pure: they read their arguments
and the module-level constants, call ``logger``, and touch nothing else. No
``shared/`` module is in the blast radius and no production file is modified —
this task adds one test file.

No assertion here reads the handler's source text. Task 9.1's file has several
(the runner construction, ``run_test``'s arity), and those are the right shape
for what they pin, but ``lambda_function.py`` is concurrently owned by task 9.3,
which is wiring ``source_data_uri`` through the constructor. A source-text
assertion added here would duplicate a fence 9.3 is actively moving.
``shadow_comparison.py`` (task 10.1) is likewise untouched.

Ledger notes — including where the ledger is silent
---------------------------------------------------

* ``LGR-b2f79ef-8`` (**template / ratified**, concept ``logging``) — the one
  record that shaped this file: a log line that is the sole carrier of a
  distinction is behaviour and stays. It is why the WARNING-versus-ERROR split
  is asserted as a property (:meth:`TestEffectiveResolution.
  test_the_two_diagnostics_are_mutually_exclusive`) rather than left to the
  example-level test.
* ``LGR-3e45e65-1`` (**template**, concept ``testing``) — the unit layer pins
  ``AWS_REGION`` in ``tests/unit/conftest.py`` by assignment, so the
  ``setdefault`` calls below are consistent no-ops, kept for module-import parity
  with every sibling in this directory.
* ``LGR-60c4985-6`` (**template**) — in this Lambda's test suite a deliberate
  hold is encoded as an assertion and *amended* rather than deleted when the
  hold is lifted. Relevant to :class:`TestGlueIsUnreachableByAccident`: flipping
  the default under R11.2 should have to edit an assertion that says why it was
  held. **Applied twice.** On the deployed-value reversal the assertion that had
  to move was the CDK pin in
  ``tests/cdk/stacks/test_verification_executor_env.py``, and nothing here —
  this file's assertions were about resolution and stayed true. On the
  code-default reversal they are no longer about a different thing: eight
  assertions in this file moved from ``pandas`` to ``glue``, and two inverted.
  Each carries its own before/after claim.
* ``why`` on ``lambda_function.py`` returns four commits and **no record naming
  the executor-selection block** — task 9.1 is not committed to main, so the
  ledger holds nothing about it. Stated as a gap, not as an all-clear. The two
  broad matches (``LGR-0176ccc-1``, commit ``2b45b72912``) carry no records and
  match nearly every path — the documented snapshot trap.
* ``decisions(concept="deployment", include_backfilled=True)`` returns two
  records, neither about this flag (``LGR-b46a5bb-3`` is the Foundation-first
  deploy order). ``decisions(concept="error-handling")`` returns one,
  ``LGR-b2f79ef-13``, whose own body records that the concept is essentially
  empty. Both areas are thin rather than settled.

Codegraph, for the symbols this reads
-------------------------------------

Line numbers re-taken on the glue-first change, since the docstrings above both
functions grew:

* ``resolve_verification_executor`` (``lambda_function.py:477``) — 1 caller,
  ``resolve_effective_executor``; flagged ⚠️ no covering tests.
* ``resolve_effective_executor`` (``:539``) — 1 caller, ``lambda_handler``;
  flagged ⚠️ no covering tests.
* ``VERIFICATION_EXECUTORS`` (``:450``) — 1 caller; ``VERIFICATION_EXECUTOR_ENV``
  (``:396``) — 3 callers; ``VERIFICATION_EXECUTOR_GLUE`` (``:437``) — 5 callers
  (was 3; the two fall-through returns are the new ones). All flagged ⚠️ no
  covering tests.

All five coverage flags are **false negatives**, verified rather than assumed:
``test_verification_executor_selection.py`` covers every one of them and was run
directly (37 of its 38 tests pass; the one failure is task 9.3's in-flight
constructor reformat, not a resolution defect). This matches the spec's standing
note that every codegraph coverage flag on ``lambda/**`` is unverified, because
the cause is ``sys.path`` manipulation plus bare-name imports that the symbol
graph does not follow.
"""

import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import assume, event, example, given, settings
from hypothesis import strategies as st

# Region/account before any boto3 import. LGR-3e45e65-1 pins the region in
# tests/unit/conftest.py, so these are consistent no-ops.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = Path(__file__).resolve().parents[4] / "lambda" / "agentic_data_ingestion_test_code"
sys.path.insert(0, str(_TEST_CODE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "shared"))

# Stub the heavy / AWS-touching imports so the handler module loads in isolation.
# The same set every sibling in this directory uses.
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
    sys.modules["lambda_function"] = _mod
    yield


_SETTINGS = settings(max_examples=300, deadline=None)

#: The vocabulary this file generates near-misses AROUND.
#:
#: Spelled out rather than read off the module, then checked against it by
#: :func:`_assert_vocabulary_matches_the_module`. Deriving the generators from
#: the implementation would make the near-miss families track a renamed member
#: silently, and a generator that follows the code it is testing cannot detect
#: the code moving. The check is the other half of that trade.
_VOCABULARY = ("pandas", "glue", "shadow")

#: ``str.strip()`` removes every one of these. ``\u00a0`` (no-break space) and
#: ``\u2009`` (thin space) are in the list for that reason: they are what a value
#: pasted out of a rendered document or a wiki table carries, and a normalisation
#: that looked like it only touched ASCII would still strip them.
_WHITESPACE = ("\u0020", "\t", "\n", "\r", "\x0b", "\x0c", "\u00a0", "\u2009", "  ", " \t")

#: Near-misses worth naming, grouped by the mistake that produces them. Every
#: entry is outside :data:`_VOCABULARY` by construction, so any of them may be
#: fed to a property that requires an unrecognised value.
_NAMED_NEAR_MISSES = (
    # Set, but to nothing. R2.3 lists empty separately from absent, and the two
    # differ in the log: this warns, `None` does not.
    "",
    " ",
    # The USE_AGENTCORE_RUNTIME vocabulary applied to the wrong flag — the most
    # likely copy-paste in this file, since that precedent is what §4.2 follows.
    "true",
    "false",
    "1",
    "0",
    # The shapes a templating layer or a shell emits for "unset".
    "none",
    "None",
    "null",
    "nil",
    "undefined",
    # Plausible spellings from an adjacent vocabulary.
    "spark",
    "pyspark",
    "glue_session",
    "glue-session",
    "GlueSessionTestRunner",
    "code_interpreter",
    # Typos.
    "gluee",
    "gule",
    "pandass",
    "panda",
    "shdow",
    "shaddow",
    # Quoted and list-ish: what a JSON-valued env block or a mis-escaped CDK
    # literal delivers. The value arrives WITH its quotes.
    '"glue"',
    "'glue'",
    "['glue']",
    "pandas,glue",
    "glue|shadow",
    "glue shadow",
    # Casing, pinned rather than left to the generator.
    "PANDAS",
    "Pandas",
    "GLUE",
    "Glue",
    "gLuE",
    "SHADOW",
    "Shadow",
    # Whitespace, pinned. A trailing space is invisible under `%s`, which is why
    # the handler logs with `%r`.
    " glue",
    "glue ",
    " glue ",
    "\tglue",
    "glue\n",
    "\u00a0glue",
    # A NUL and a stray control character — what a truncated or mis-decoded read
    # of the environment produces.
    "glue\x00",
    "glue\x7f",
)

#: A well-formed Verification_Session_Role ARN. Only its truthiness is
#: load-bearing — see
#: :meth:`TestEffectiveResolution.test_the_role_guard_tests_presence_not_validity`.
_ROLE = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"


@st.composite
def _casing_near_misses(draw):
    """A vocabulary member with an arbitrary subset of its letters upper-cased.

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
    """A vocabulary member with whitespace added on one or both sides.

    ``assume`` discards the empty/empty draw for the same reason as above.
    """
    member = draw(st.sampled_from(_VOCABULARY))
    leading = draw(st.sampled_from(("", *_WHITESPACE)))
    trailing = draw(st.sampled_from(("", *_WHITESPACE)))
    candidate = f"{leading}{member}{trailing}"
    assume(candidate not in _VOCABULARY)
    return candidate


@st.composite
def _interior_near_misses(draw):
    """A vocabulary member with a character inserted strictly inside it.

    Distinct from the whitespace family: neither trimming nor lower-casing
    recovers the member, so these stay refused under *both* normalisations §4.2
    rejected. They are here to keep the "outside the set" population from being
    made entirely of shapes a ``.strip()`` would rescue — otherwise a resolver
    that normalised would look better against this file than it deserves.

    The insertion position excludes both ends deliberately. Inserting at
    position 0 or at the end with a whitespace character would reproduce a
    :func:`_whitespace_near_misses` draw, and that draw *is* recoverable by
    trimming — which would falsify the paragraph above.
    """
    member = draw(st.sampled_from(_VOCABULARY))
    position = draw(st.integers(min_value=1, max_value=len(member) - 1))
    inserted = draw(st.sampled_from(("_", "-", ".", " ", "x", "0")))
    return f"{member[:position]}{inserted}{member[position:]}"


#: Arbitrary configured values, over the declared domain ``str | None``.
#:
#: Includes the recognised members and ``None`` on purpose: the totality
#: properties are statements about the WHOLE domain, so the strategy that feeds
#: them must be able to draw the accepted values too.
_arbitrary_raw = st.one_of(
    st.none(),
    st.sampled_from(_VOCABULARY),
    st.sampled_from(_NAMED_NEAR_MISSES),
    st.text(max_size=12),
    _casing_near_misses(),
    _whitespace_near_misses(),
    _interior_near_misses(),
)

#: Configured values that are strings and are outside the vocabulary.
#:
#: Never ``None``: absence is silent and recognised values are returned
#: unchanged, so both would falsify the warning properties for the wrong reason.
_outside_vocabulary = st.one_of(
    st.sampled_from(_NAMED_NEAR_MISSES),
    st.text(max_size=12).filter(lambda value: value not in _VOCABULARY),
    _casing_near_misses(),
    _whitespace_near_misses(),
    _interior_near_misses(),
)

#: Role ARNs, including both falsy spellings the guard collapses.
_role_arns = st.one_of(
    st.none(),
    st.just(""),
    st.just(_ROLE),
    st.text(min_size=1, max_size=10),
)


def _assert_vocabulary_matches_the_module():
    """Guard the generators against the implementation moving underneath them.

    Every "outside the vocabulary" property in this file is only as strong as
    :data:`_VOCABULARY` being the real vocabulary. If a member were renamed, the
    near-miss families would keep generating around the old name and the
    properties would keep passing while testing nothing — the fail-open shape.
    """
    assert set(_VOCABULARY) == set(_mod.VERIFICATION_EXECUTORS), (
        f"this file generates near-misses around {sorted(_VOCABULARY)} but the module's vocabulary is "
        f"{sorted(_mod.VERIFICATION_EXECUTORS)}; the generated populations no longer describe it"
    )
    assert all(value not in _mod.VERIFICATION_EXECUTORS for value in _NAMED_NEAR_MISSES), (
        "a named near-miss has become a recognised value"
    )


@contextmanager
def _captured_records():
    """Collect the records the handler's own logger emits during the block.

    A per-call ``logging.Handler`` on ``lambda_function.logger`` rather than
    ``caplog``: ``caplog`` is function-scoped, so under ``@given`` it is applied
    once for the whole function and accumulates across examples. This is
    per-example by construction and still goes through real record creation, so
    ``record.getMessage()`` renders exactly what a log reader sees — including
    the ``%r`` that makes a trailing space visible.

    No level manipulation: WARNING and ERROR both exceed the Powertools
    ``Logger`` default of INFO.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collector(level=logging.DEBUG)
    _mod.logger.addHandler(handler)
    try:
        yield records
    finally:
        _mod.logger.removeHandler(handler)


def _messages_at(records: list[logging.LogRecord], level: str) -> list[str]:
    """Rendered messages at *level*, in emission order."""
    return [record.getMessage() for record in records if record.levelname == level]


def _over_named_near_misses(test):
    """Stack one ``@example`` per entry of :data:`_NAMED_NEAR_MISSES`.

    The named families are the ones the task enumerates as load-bearing, so
    their coverage is pinned rather than left to the seed. ``_outside_vocabulary``
    samples from the same tuple and reaches all of it over 300 draws, but
    "reaches with high probability" is not "always reaches".
    """
    for value in reversed(_NAMED_NEAR_MISSES):
        test = example(raw=value)(test)
    return test


# ══════════════════════════════════════════════════════════════════════
# The vocabulary the generators are built around
# ══════════════════════════════════════════════════════════════════════


def test_the_generated_populations_describe_the_modules_vocabulary():
    """The control for every "outside the vocabulary" property below."""
    _assert_vocabulary_matches_the_module()


# ══════════════════════════════════════════════════════════════════════
# Totality — R2.3
# ══════════════════════════════════════════════════════════════════════


class TestResolutionIsTotal:
    """**Validates: Requirements 2.1, 2.2, 2.3**"""

    @given(raw=_arbitrary_raw)
    @_SETTINGS
    def test_every_configured_value_resolves_into_the_vocabulary(self, raw):
        """No input escapes the three-value vocabulary.

        The resolved value is the executor label the log and the report carry, so
        anything outside the set is a label no consumer knows how to read.
        """
        resolved = _mod.resolve_verification_executor(raw)
        event(f"resolved={resolved}")
        assert resolved in _mod.VERIFICATION_EXECUTORS, f"{raw!r} resolved to {resolved!r}, outside the vocabulary"

    @given(raw=_arbitrary_raw)
    @_SETTINGS
    def test_the_result_is_either_the_input_or_the_glue_default(self, raw):
        """The whole mapping in one statement: identity on the set, else ``glue``.

        Stronger than "lands in the vocabulary", which a resolver that always
        returned ``shadow`` would also satisfy.

        **Amended, not deleted** (``LGR-60c4985-6``): the second disjunct was
        ``VERIFICATION_EXECUTOR_PANDAS`` and the method was named
        ``..._the_pandas_default``. The shape of the property is unchanged —
        identity on the vocabulary, one fixed fall-through for everything else —
        and only which member the fall-through names has moved.
        """
        resolved = _mod.resolve_verification_executor(raw)
        assert resolved == raw or resolved == _mod.VERIFICATION_EXECUTOR_GLUE, (
            f"{raw!r} resolved to {resolved!r}, which is neither the input nor the default"
        )

    @given(raw=_arbitrary_raw)
    @_SETTINGS
    def test_resolution_is_idempotent(self, raw):
        """Resolving a resolved value changes nothing.

        Follows from identity-on-the-set, and is asserted separately because it
        is the form a caller relies on: a value that has already been through
        the resolver can be passed through again — as the effective resolver in
        fact does — without a second downgrade.
        """
        once = _mod.resolve_verification_executor(raw)
        assert _mod.resolve_verification_executor(once) == once

    @given(raw=_arbitrary_raw)
    @_SETTINGS
    def test_recognised_values_are_returned_unchanged(self, raw):
        """R2.1, R2.2 — ``glue`` and ``shadow`` are reachable at all.

        The half of the property that stops "default safely" from being
        satisfied by a resolver that returns ``pandas`` unconditionally.
        """
        if raw in _mod.VERIFICATION_EXECUTORS:
            assert _mod.resolve_verification_executor(raw) == raw


# ══════════════════════════════════════════════════════════════════════
# Defaulting — R2.3
# ══════════════════════════════════════════════════════════════════════


class TestUnrecognisedValuesDefaultToGlue:
    """**Validates: Requirement 2.3** (as reversed by the glue-first decision)

    **Renamed from ``TestUnrecognisedValuesDefaultToPandas``**, with every
    assertion inside amended rather than deleted (``LGR-60c4985-6``). The class
    is about the fall-through, and the fall-through moved; the *structure* of
    what it asserts — one fixed destination for everything outside the
    vocabulary, plus a WARNING for every string that reaches it — is unchanged.
    """

    @_over_named_near_misses
    @given(raw=_outside_vocabulary)
    @_SETTINGS
    def test_any_value_outside_the_vocabulary_resolves_to_glue(self, raw):
        """R2.3, reversed — the fall-through is ``glue``.

        **Amended from ``== "pandas"``**, whose rationale was "today's behaviour
        is the fall-through". It no longer is: the fall-through is now the
        *expensive* executor, so every value this property generates would create
        a billable Glue interactive session on a provisioned stack. That cost was
        accepted explicitly in exchange for "it should always be glue first".
        """
        assert _mod.resolve_verification_executor(raw) == "glue"

    def test_absence_resolves_to_glue_silently(self):
        """R2.3 for the absent case.

        Separated from the string cases because it is the one unrecognised input
        that does NOT warn: an unset variable is the documented default state,
        and warning on it would put a line in the log for every correctly
        deployed run.

        **Amended from ``== "pandas"``.** The silence is deliberately unchanged
        and is now better justified, not worse: with ``glue`` as the fall-through,
        an unset variable produces exactly the intended executor, so there is
        nothing to report. ``LGR-0fe3e5d-2`` (template / ratified) cites the
        sibling example test as this module's statement that a correctly
        configured run logs nothing; that statement still holds.
        """
        with _captured_records() as records:
            assert _mod.resolve_verification_executor(None) == "glue"
        assert _messages_at(records, "WARNING") == []

    @given(raw=_casing_near_misses())
    @_SETTINGS
    def test_a_casing_near_miss_is_refused(self, raw):
        """Case-insensitive matching was REJECTED (design §4.2, 9.1's docstring).

        ``"GLUE"`` is not ``glue``. Accepting it would remove the only signal
        that the deployed value is not the intended one, so this assertion is
        the fence against a later ``.lower()``.

        **Amended from ``== "pandas"`` to ``== "glue"``, and the fence is
        UNAFFECTED.** ``LGR-ad78946-1``'s reasoning is about normalisation, not
        about the fall-through's destination. The reason a ``.lower()`` still
        breaks this test is ``"PANDAS"`` and ``"SHADOW"``: under normalisation
        they would resolve to ``pandas`` and ``shadow``, and here they must
        resolve to ``glue``. Do NOT add ``.strip()`` or ``.lower()``.
        """
        event(f"casing near-miss for {raw.lower()}")
        assert _mod.resolve_verification_executor(raw) == "glue"
        # The fence, stated where it bites: the lower-cased form WOULD be a
        # member, so a `.lower()` would send this input somewhere else.
        assert raw.lower() in _VOCABULARY

    @given(raw=_whitespace_near_misses())
    @_SETTINGS
    def test_a_whitespace_near_miss_is_refused(self, raw):
        """``.strip()`` was REJECTED for the same reason.

        Includes the non-ASCII spaces ``str.strip()`` also removes, so a
        normalisation added "just for ASCII spaces" is caught too.

        **Amended from ``== "pandas"`` to ``== "glue"``.** This is the assertion
        that keeps the no-normalisation fence honest after the reversal, and it
        is worth being explicit about why: ``" glue"`` resolving to ``glue``
        would look identical under normalisation, but ``"shadow "`` and
        ``" pandas"`` must resolve to ``glue`` and NOT to the trimmed member. The
        second assertion below pins that these inputs are genuinely one trim away
        from a member, so the property is not satisfied vacuously.
        """
        event(f"whitespace near-miss for {raw.strip()}")
        assert _mod.resolve_verification_executor(raw) == "glue"
        # The near-miss is genuinely one: trimming it WOULD recover a member.
        assert raw.strip() in _VOCABULARY

    @given(raw=_interior_near_misses())
    @_SETTINGS
    def test_an_interior_near_miss_is_refused(self, raw):
        """A member with a character inserted inside it stays refused.

        Unlike the whitespace family, neither trimming nor lower-casing recovers
        these — asserted below rather than claimed — so the refused population is
        not made entirely of shapes a normalisation would rescue.

        **Amended from ``== "pandas"`` to ``== "glue"``.** These are the inputs
        for which "refused" and "resolved to glue" are genuinely the same
        observation, since no normalisation recovers a member from them.
        """
        assert _mod.resolve_verification_executor(raw) == "glue"
        assert raw.strip().lower() not in _VOCABULARY


# ══════════════════════════════════════════════════════════════════════
# The warning — R2.4
# ══════════════════════════════════════════════════════════════════════


class TestTheWarningNamesTheValue:
    """**Validates: Requirement 2.4**"""

    def test_the_capture_harness_is_not_failing_open(self):
        """Positive control for :func:`_captured_records`.

        Every "emits no warning" assertion in this class is vacuous if the
        capture collects nothing, and a broken capture would leave only the
        "emits one warning" assertions failing — which reads as a resolution
        defect rather than as a harness defect. ``LGR-b2f79ef-8`` is the
        precedent for treating a log line as behaviour; a control is what makes
        asserting on one honest.
        """
        with _captured_records() as records:
            _mod.resolve_verification_executor("definitely-not-an-executor")
        assert len(_messages_at(records, "WARNING")) == 1

        with _captured_records() as records:
            _mod.resolve_effective_executor("glue", "")
        assert len(_messages_at(records, "ERROR")) == 1

    @_over_named_near_misses
    @given(raw=_outside_vocabulary)
    @_SETTINGS
    def test_exactly_one_warning_names_the_value_it_received(self, raw):
        """R2.4 — silently accepting a typo is correct, but it must be visible.

        Three separate claims, because each fails differently: exactly one
        record (not zero, not one per candidate member), the received value
        rendered with ``repr`` so a trailing space or a NUL is legible, and the
        variable name so the reader knows which knob to turn.

        **The resolved value is amended from ``pandas`` to ``glue``; the three
        WARNING claims are unchanged and now carry more weight.** They are the
        entire mitigation for the reversal's accepted cost: a typo no longer
        leaves behaviour where it was, it escalates the run onto real Spark and
        bills for it, so this line is the only thing distinguishing "the operator
        asked for glue" from "the operator asked for something else and got glue".
        """
        with _captured_records() as records:
            assert _mod.resolve_verification_executor(raw) == "glue"

        warnings = _messages_at(records, "WARNING")
        assert len(warnings) == 1, f"{raw!r} produced {len(warnings)} warnings"
        assert repr(raw) in warnings[0], f"{raw!r} is not named in {warnings[0]!r}"
        assert "VERIFICATION_EXECUTOR" in warnings[0]

    @given(raw=st.one_of(st.none(), st.sampled_from(_VOCABULARY)))
    @_SETTINGS
    def test_a_correctly_configured_value_warns_nothing(self, raw):
        """The converse of R2.4, and what keeps the warning worth reading.

        A warning on every run trains the reader to ignore the one case R2.4
        exists for.
        """
        with _captured_records() as records:
            _mod.resolve_verification_executor(raw)
        assert _messages_at(records, "WARNING") == []

    @_over_named_near_misses
    @given(raw=_outside_vocabulary)
    @_SETTINGS
    def test_an_unrecognised_value_is_a_warning_and_not_an_error(self, raw):
        """A typo is not a misconfiguration the deployment cannot satisfy.

        R2.4's WARNING means "outside the vocabulary". ERROR is reserved for
        ``glue`` requested with no role ARN — a correctly spelled request that
        cannot be honoured. At one level the two are indistinguishable in the
        log, which is the distinction ``LGR-b2f79ef-8`` is about.
        """
        with _captured_records() as records:
            _mod.resolve_verification_executor(raw)
        assert _messages_at(records, "ERROR") == []


# ══════════════════════════════════════════════════════════════════════
# R11.2 — the default cannot be escaped by accident
# ══════════════════════════════════════════════════════════════════════


class TestGlueIsUnreachableByAccident:
    """**Validates: Requirements 2.1, 11.2** — and records where 11.2 was reversed.

    **The class name is now historical, and is kept for that reason.** Glue IS
    reachable by accident: it is the fall-through, so an unset variable, an empty
    string, a typo, a casing slip and a whitespace near-miss all select it. The
    name is left in place, with the reversal explained here, because renaming it
    would erase the fact that this file once guaranteed the opposite —
    ``LGR-60c4985-6``'s whole point is that a lifted hold stays legible as a
    decision rather than reading as a regression.

    **Two reversals, in two steps.** First the *deployed* value went to ``glue``
    in both stacks (``LGR-87b8538-1``), which this class's assertions survived
    untouched — they were about resolution, not about deployment. Then the *code
    fall-through* went to ``glue`` (this change), which they do not survive: the
    property "no input other than the exact string ``glue`` resolves to ``glue``"
    is now false by decision, not by defect.

    Per ``LGR-60c4985-6`` it is amended, not deleted. What is asserted instead is
    the half that still holds and is still worth guaranteeing — nothing reaches
    ``pandas`` or ``shadow`` except its own exact spelling — plus the WARNING that
    is now the only signal a misconfigured stack gives.

    R11.2's precondition remains unmet: R11.1's concurrent-session measurement was
    deliberately never taken, because the task 1.3 probe creates billable
    sessions. ``LGR-87b8538-1`` tolerated the first reversal partly because
    "R2.3's fall-through to pandas is a second layer, unchanged"; this change
    removes that second layer, so the accepted deviation is wider than the one
    that record ratified. The CDK-side pins remain where they can see the deployed
    value: ``tests/cdk/stacks/test_verification_executor_env.py`` and
    ``test_verification_session_role_arn_env.py``.
    """

    @given(raw=_arbitrary_raw)
    @_SETTINGS
    def test_nothing_reaches_pandas_or_shadow_by_accident(self, raw):
        """**Replaces** ``test_only_the_exact_spelling_resolves_to_glue``.

        That test asserted the converse of this one — "No typo, casing slip or
        stray space escalates a run onto real Spark" — and that claim is now false
        by decision: every one of those inputs escalates onto real Spark, because
        Spark is the default. It is replaced rather than removed, and the
        replacement is the strongest statement still true about the resolver's
        near-miss handling:

        * a value resolving to ``pandas`` was spelled exactly ``pandas``, and
        * a value resolving to ``shadow`` was spelled exactly ``shadow``,

        which together are what still forbids a ``.strip()`` or a ``.lower()``
        (``LGR-ad78946-1``): under either, ``" pandas"`` or ``"SHADOW"`` would
        select an executor nobody asked for. The old property was one guard
        against normalisation; this is the same guard, stated on the two members
        that are no longer the fall-through.
        """
        resolved = _mod.resolve_verification_executor(raw)
        if resolved == "pandas":
            assert raw == "pandas", f"{raw!r} reached the pandas proxy without being spelled `pandas`"
        if resolved == "shadow":
            assert raw == "shadow", f"{raw!r} reached shadow mode without being spelled `shadow`"

    @given(raw=_arbitrary_raw)
    @_SETTINGS
    def test_only_the_exact_spelling_resolves_to_shadow(self, raw):
        """The same guard on the third state.

        Shadow mode runs the Glue executor as well as the pandas one (R4.1), so
        reaching it by accident carries the same session cost as reaching
        ``glue`` by accident.

        **Unchanged by the reversal — verified, not assumed.** ``shadow`` was
        never the fall-through and is not one now, so nothing about this property
        moved. Kept alongside
        :meth:`test_nothing_reaches_pandas_or_shadow_by_accident`, which subsumes
        it, because this is the one that carries the session-cost reason.
        """
        if _mod.resolve_verification_executor(raw) == "shadow":
            assert raw == "shadow", f"{raw!r} reached shadow mode without being spelled `shadow`"

    @given(raw=_outside_vocabulary, role_arn=_role_arns)
    @_SETTINGS
    def test_an_unrecognised_value_now_does_reach_a_glue_session(self, raw, role_arn):
        """**Inverts** ``test_an_unrecognised_value_never_reaches_a_glue_session``.

        That test asserted ``resolve_effective_executor(raw, role_arn) ==
        "pandas"`` for every unrecognised value and every role ARN, reasoning that
        "a role ARN being present must not rescue an unrecognised executor value:
        the two configuration errors are independent". **That is the single claim
        this change most directly reverses, and it is reversed knowingly.**

        A role ARN being present now DOES carry an unrecognised value onto real
        Spark, because the value resolves to ``glue`` before the role guard is
        reached. The concrete consequence: a typo in ``VERIFICATION_EXECUTOR`` on
        a correctly provisioned stack creates a billable Glue interactive session.
        Keeping unrecognised values on ``pandas`` was the recommended alternative
        and the user overruled it — "it should always be glue first" — so this is
        a deliberate call and not an oversight.

        What is asserted instead is the resulting behaviour in full, including the
        two-branch shape the role guard gives it, so a future reader can see
        exactly which configurations bill.
        """
        resolved = _mod.resolve_effective_executor(raw, role_arn)
        if role_arn:
            assert resolved == "glue", f"{raw!r} with a role ARN should now select glue, got {resolved!r}"
        else:
            # No role ARN: `role_arn` is keyword-only and required on
            # GlueSessionTestRunner, so there is no session to create and the
            # downgrade to pandas stands (`LGR-ad78946-5`). Deliberately NOT
            # changed to glue by this reversal.
            assert resolved == "pandas", f"{raw!r} with no role ARN must still downgrade, got {resolved!r}"


# ══════════════════════════════════════════════════════════════════════
# The effective resolution — totality including the role guard
# ══════════════════════════════════════════════════════════════════════


class TestEffectiveResolution:
    """**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

    ``resolve_effective_executor`` answers "what can be built" rather than "what
    was asked for". Task 9.1 records the downgrade from ``glue`` with no role
    ARN as a **known, acknowledged deviation from R2.1's literal wording**,
    offered for ratification rather than hidden: R2.1 says ``glue`` SHALL verify
    with the Glue runner, and this returns ``pandas`` when there is no role to
    pass. The behaviour as implemented is what is encoded here.
    """

    @given(raw=_arbitrary_raw, role_arn=_role_arns)
    @_SETTINGS
    def test_effective_resolution_is_total_over_both_arguments(self, raw, role_arn):
        """Every combination lands inside the vocabulary."""
        resolved = _mod.resolve_effective_executor(raw, role_arn)
        event(f"effective={resolved}")
        assert resolved in _mod.VERIFICATION_EXECUTORS

    @given(raw=_arbitrary_raw, role_arn=_role_arns)
    @_SETTINGS
    def test_glue_is_selected_exactly_when_it_is_requested_and_a_role_exists(self, raw, role_arn):
        """The whole role guard, in both directions.

        Encodes the acknowledged deviation: a resolved ``glue`` with a falsy role
        ARN resolves to ``pandas``. Both falsy spellings — ``""`` and ``None`` —
        collapse the same way, because the guard is ``not session_role_arn``.

        **Amended, not deleted** (``LGR-60c4985-6``). The antecedent was
        ``raw == "glue"``; it is now ``raw not in ("pandas", "shadow")``, which is
        the same statement written against the new fall-through. Those two are the
        only values that resolve to anything other than ``glue``, so everything
        else — including ``None`` and every typo — is a ``glue`` request as far as
        the role guard is concerned. The guard itself is untouched.
        """
        selected_glue = _mod.resolve_effective_executor(raw, role_arn) == "glue"
        requests_glue = raw not in ("pandas", "shadow")
        assert selected_glue == (requests_glue and bool(role_arn)), (
            f"raw={raw!r} role_arn={role_arn!r} selected_glue={selected_glue}"
        )

    @given(role_arn=_role_arns)
    @_SETTINGS
    def test_shadow_survives_the_role_guard(self, role_arn):
        """``shadow`` is not downgraded by a missing role ARN.

        Its authoritative half is the pandas proxy (R4.1), which needs no role.
        Downgrading it would hand task 10.3 a state where ``shadow`` had already
        been rewritten to ``pandas``, so the comparison would never be
        attempted — and the two would be indistinguishable at the call site.
        """
        assert _mod.resolve_effective_executor("shadow", role_arn) == "shadow"

    @given(raw=_arbitrary_raw, role_arn=_role_arns)
    @_SETTINGS
    def test_the_two_diagnostics_stay_attributable_but_can_now_co_occur(self, raw, role_arn):
        """**Amended: the mutual exclusivity this asserted is gone.**

        This was ``test_the_two_diagnostics_are_mutually_exclusive``, asserting
        ``not (warnings and errors)`` on the stated mechanism that "an
        unrecognised value has already become ``pandas`` by the time the role
        guard runs, so it cannot reach the ERROR arm". The glue-first reversal
        falsifies that mechanism: an unrecognised value now becomes ``glue``, so it
        reaches the guard, and a typo on a stack with no
        ``VERIFICATION_SESSION_ROLE_ARN`` emits BOTH lines.

        Kept as an assertion rather than deleted, and *strengthened* where it
        could be. What ``LGR-b2f79ef-8`` (template / ratified) and
        ``LGR-ad78946-5`` (template / ratified) actually require is that the two
        facts stay **attributable** — one names a value outside the vocabulary,
        the other names a request the deployment cannot satisfy — not that they
        never co-occur. So each level's exact condition is still asserted in both
        directions, and each message is now asserted to name its own variable, so
        that co-occurring lines are still individually readable.
        """
        with _captured_records() as records:
            resolved = _mod.resolve_effective_executor(raw, role_arn)

        warnings = _messages_at(records, "WARNING")
        errors = _messages_at(records, "ERROR")

        expected_warning = raw is not None and raw not in _mod.VERIFICATION_EXECUTORS
        # Every value but an exact `pandas` or `shadow` now resolves to `glue`,
        # so every one of them can reach the role guard.
        expected_error = raw not in ("pandas", "shadow") and not role_arn
        assert bool(warnings) == expected_warning, f"raw={raw!r} warnings={warnings}"
        assert bool(errors) == expected_error, f"raw={raw!r} role_arn={role_arn!r} errors={errors}"

        # Attributability, which is what the exclusivity claim was standing in
        # for: each line names the variable its own fact is about, so the two
        # remain distinguishable when both are emitted.
        if warnings:
            assert repr(raw) in warnings[0], f"the WARNING does not name {raw!r}: {warnings[0]!r}"
        if errors:
            assert "VERIFICATION_SESSION_ROLE_ARN" in errors[0]
            assert resolved == "pandas"

    @given(role_arn=st.text(min_size=1, max_size=12))
    @_SETTINGS
    def test_the_role_guard_tests_presence_not_validity(self, role_arn):
        """Any non-empty string satisfies the guard, including a non-ARN.

        Recorded as behaviour rather than reported as a defect. The guard is
        ``not session_role_arn`` — a truthiness check — so ARN *validity* is left
        to the Glue API, which is the only authority on whether the role exists
        and is assumable. A client-side ARN validator would be a second thing to
        keep in step with IAM, and it would still not answer that question.
        """
        assert _mod.resolve_effective_executor("glue", role_arn) == "glue"
