# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for Verification_Executor resolution and dispatch (p42 task 9.1).

Covers requirements 2.1, 2.2, 2.3, 2.4 and 11.2 by example. The exhaustive
property — that resolution is total over arbitrary strings, including casing
and whitespace near-misses — is p42 task 9.2 and lives in its own file; these
are the named cases and the dispatch wiring.

**What this file is actually fencing.** Before task 9.1 nothing in production
imported or constructed ``GlueSessionTestRunner`` — only comments mentioned it.
So there are two distinct things worth asserting and they fail differently:

1. ``resolve_verification_executor`` maps a raw configured value onto the
   three-value vocabulary, defaulting to ``glue``. A regression here is a
   wrong verdict source. **The default was ``pandas`` and is now ``glue``** —
   verification is glue-first, so ``pandas`` and ``shadow`` are reachable only
   by their own exact spelling and everything else (unset, empty, typo, casing
   slip, whitespace near-miss) lands on real Spark. Every assertion below that
   encoded the old landing spot is amended in place with its reversal noted,
   per ``LGR-60c4985-6``, rather than deleted.
2. The handler's Phase 2 dispatch actually constructs the runner the resolution
   names. A regression here is a resolution that resolves correctly and then
   changes nothing — the same inert-flag failure mode design §4.2's MECHANISM
   note describes for the environment variable itself.

The dispatch assertions patch ``glue_session_runner`` on ``sys.modules`` rather
than patching an attribute of ``lambda_function``, because the import is
function-local on the ``glue`` branch (mirroring the ``agentcore_proxy`` import
on the ``USE_AGENTCORE_RUNTIME`` branch) and therefore has no module attribute
to patch.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Region/account before any boto3 import
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy / AWS-touching imports so the module loads in isolation. Same set
# the sibling handler tests use. `glue_session_runner` is deliberately NOT
# stubbed here: the dispatch tests below patch it per-test so that the
# unpatched default path is genuinely the one production takes.
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


# ──────────────────────────────────────────────────────────────────────────
# The vocabulary itself
# ──────────────────────────────────────────────────────────────────────────


def test_vocabulary_is_exactly_three_values():
    """R2 is three-valued. Two booleans cannot express three states.

    Pinned as a set comparison rather than a length check so that swapping a
    member for a new one fails here instead of passing quietly.
    """
    assert sorted(_mod.VERIFICATION_EXECUTORS) == ["glue", "pandas", "shadow"]


def test_the_code_fall_through_is_glue():
    """R2.3, **reversed** — an ABSENT configuration value resolves to ``glue``.

    **Amended, not deleted** (``LGR-60c4985-6``, template / ratified: an
    assertion encoding a deliberate hold is changed on purpose and the change
    stays legible as one). This asserted
    ``resolve_verification_executor(None) == "pandas"`` under the name
    ``test_default_is_pandas_while_the_concurrency_measurement_is_absent``.
    Both the name and the second assertion are reversed by the user's decision
    that verification is glue-first: "it should always be glue first."

    The two defaults the previous docstring separated are still two things, and
    the distinction is worth keeping because the *reason* they now agree is not
    that either was derived from the other:

    * The **code fall-through**, which is what the second assertion tests. It
      was ``pandas`` and is now ``glue``. That is this change.
    * The **deployed value** of ``VERIFICATION_EXECUTOR``, which is what R11.2
      spoke about and which no assertion in this file can see. Both stacks
      already published ``glue`` before this change (``LGR-87b8538-1``); the
      pin lives in ``tests/cdk/stacks/test_verification_executor_env.py``.

    R11.2's precondition is still unmet — R11.1's concurrent-session
    measurement was deliberately never taken, because the p42 task 1.3 probe
    creates billable sessions — so the deployed value remains an accepted
    deviation. The fall-through asserted here **widens** that deviation rather
    than cushioning it: the safe landing this test used to guarantee is gone, so
    a misconfigured stack now bills for a Glue session instead of quietly
    running the proxy. That cost was accepted explicitly. What is left as the
    mitigation is the WARNING, asserted below, plus the
    ``session_capacity_exhausted`` abstention path.

    The first assertion is unchanged: the constant still spells ``pandas``, and
    ``pandas`` is still reachable — by its own exact spelling, and by
    :func:`resolve_effective_executor`'s no-role-ARN downgrade.
    """
    assert _mod.VERIFICATION_EXECUTOR_PANDAS == "pandas"
    assert _mod.resolve_verification_executor(None) == "glue"


# ──────────────────────────────────────────────────────────────────────────
# Resolution (R2.1-R2.4)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["pandas", "glue", "shadow"])
def test_each_recognised_value_resolves_to_itself(value):
    """R2.1, R2.2 — a member of the vocabulary is returned unchanged."""
    assert _mod.resolve_verification_executor(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",  # set, but to nothing
        "PANDAS",  # casing near-miss
        "Glue",
        " glue",  # leading whitespace
        "glue ",  # trailing whitespace — invisible in a `%s` log
        "gluee",  # typo
        "spark",  # plausible but wrong vocabulary
        "true",  # the USE_AGENTCORE_RUNTIME vocabulary, wrong flag
    ],
)
def test_unrecognised_values_resolve_to_glue(value):
    """R2.3, **reversed** — empty or outside the set falls through to ``glue``.

    **Amended, not deleted** (``LGR-60c4985-6``). This was
    ``test_unrecognised_values_resolve_to_pandas``, asserting ``== "pandas"``,
    with the rationale "falls through to today's behaviour". Reversed by the
    glue-first decision.

    Read the parametrize list with the new cost in mind: every one of these
    eight values now produces a billable Glue interactive session. ``" glue"``
    and ``"glue "`` reaching ``glue`` is a coincidence of the fall-through and
    NOT normalisation — ``"PANDAS"`` and ``" glue"`` take the identical arm, and
    ``"shadow "`` (covered exhaustively by the sibling property file) reaches
    ``glue`` rather than the trimmed ``shadow``. The no-normalisation fence
    (``LGR-ad78946-1``) is intact; only the landing spot moved.
    """
    assert _mod.resolve_verification_executor(value) == "glue"


@pytest.mark.parametrize("value", ["", "GLUE", " glue", "gluee", "spark"])
def test_unrecognised_values_are_named_in_a_warning(value, caplog):
    """R2.4 — accepting a typo is correct, but it must be visible.

    Asserts the received value appears in the message, which is the part that
    makes the log actionable. ``repr`` is what the handler formats with, so a
    whitespace near-miss is legible in the record.

    **The resolved value is amended from ``pandas`` to ``glue``; the WARNING
    assertions are unchanged and now matter more.** Under the old fall-through
    a typo left behaviour where it already was, so the warning was advisory.
    Now a typo escalates the run onto real Spark and bills for it, so this is
    the only signal separating "the operator meant glue" from "the operator
    typed something else and got glue anyway". It is the whole mitigation for
    the cost this reversal accepts.
    """
    with caplog.at_level("WARNING"):
        assert _mod.resolve_verification_executor(value) == "glue"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert repr(value) in message
    assert "VERIFICATION_EXECUTOR" in message


@pytest.mark.parametrize("value", ["pandas", "glue", "shadow", None])
def test_recognised_values_and_absence_log_no_warning(value, caplog):
    """A correctly-configured run puts no line in the log.

    ``None`` is the unset default rather than a mistake; warning on it would
    emit a warning for every correctly-deployed run, which trains readers to
    ignore the one case R2.4 cares about.

    **Unchanged by the glue-first reversal, deliberately.** ``None`` now
    resolves to ``glue`` rather than ``pandas``, but it stays SILENT: an unset
    variable is now even more clearly the documented normal state than it was.
    ``LGR-0fe3e5d-2`` (template / ratified) cites this test by line as the
    module's statement that "a correctly-configured run puts no line in the
    log", so keeping it silent keeps that citation true.
    """
    with caplog.at_level("WARNING"):
        _mod.resolve_verification_executor(value)
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_resolution_is_closed_over_its_own_output():
    """Resolving a resolved value is a no-op.

    Guards the fall-through against returning something outside the vocabulary
    — which would make the executor label on the log and the report a value no
    consumer knows.
    """
    for raw in ["pandas", "glue", "shadow", "", None, "nonsense"]:
        once = _mod.resolve_verification_executor(raw)
        assert once in _mod.VERIFICATION_EXECUTORS
        assert _mod.resolve_verification_executor(once) == once


# ──────────────────────────────────────────────────────────────────────────
# Dispatch — the resolution has to reach a runner
# ──────────────────────────────────────────────────────────────────────────


_ROLE = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"


@pytest.mark.parametrize(
    ("requested", "role_arn"),
    [
        # An exact `pandas` — the deliberate opt-out, and now the ONLY way to
        # ask for the proxy by name.
        ("pandas", ""),
        ("pandas", _ROLE),
        # `glue` asked for, but no runtime role to pass. Falls back rather than
        # failing the step, because `role_arn` is keyword-only and required on
        # `GlueSessionTestRunner` — there is no session to create.
        ("glue", ""),
        ("glue", None),
        # AMENDED, not deleted (`LGR-60c4985-6`). These two used to be here as
        # "not glue, so pandas": absence and an unrecognised value both resolved
        # to `pandas` before the role guard was reached. They now resolve to
        # `glue` first and reach `pandas` only through the SAME no-role-ARN
        # downgrade as the two rows above — a different mechanism with the same
        # answer, and one that logs at ERROR where it used to log nothing (for
        # `None`) or only a WARNING (for `"nonsense"`).
        (None, ""),
        ("nonsense", ""),
    ],
)
def test_effective_executor_is_pandas_for_these_configurations(requested, role_arn):
    """The configurations that still land on the ``pandas`` label.

    ``shadow`` is deliberately absent from this list. It resolves to its OWN
    label — see :func:`test_shadow_survives_the_role_guard` — and is mapped onto
    the pandas proxy by the DISPATCH rather than by the resolution. Collapsing
    the two would be the boolean design §4.2 rejected: it would leave task 10.3
    unable to tell "pandas was asked for" from "shadow was asked for".

    **Two rows moved and one left the list entirely.** ``("nonsense", _ROLE)``
    used to be here and is now asserted in
    :func:`test_an_unrecognised_value_with_a_role_arn_now_selects_glue`, because
    a role ARN no longer fails to rescue an unrecognised value — it selects the
    Glue runner. That is the cost the glue-first decision accepts, stated where
    it can be seen rather than removed.
    """
    assert _mod.resolve_effective_executor(requested, role_arn) == "pandas"


def test_an_unrecognised_value_with_a_role_arn_now_selects_glue():
    """**Amended from an assertion that said the opposite** (``LGR-60c4985-6``).

    ``("nonsense", _ROLE)`` was a row in
    :func:`test_effective_executor_is_pandas_for_these_configurations`, whose
    reasoning was that "a role ARN being present must not rescue an unrecognised
    executor value: the two configuration errors are independent". That claim is
    now false by decision, and it is kept visible here rather than deleted.

    A typo in ``VERIFICATION_EXECUTOR`` on a correctly-provisioned stack now
    creates a billable Glue interactive session. The user was offered the
    alternative — keep unrecognised values on ``pandas`` — and overruled it: "it
    should always be glue first." The only remaining signal that the deployed
    value is not the intended one is the WARNING
    (:func:`test_unrecognised_values_are_named_in_a_warning`), which is why the
    no-normalisation fence (``LGR-ad78946-1``) must not be softened.
    """
    assert _mod.resolve_effective_executor("nonsense", _ROLE) == "glue"


def test_glue_is_selected_by_absence_as_well_as_by_name():
    """R2.1 — the flag can select the Glue runner, and so can not setting it.

    **Amended, not deleted** (``LGR-60c4985-6``). This was
    ``test_glue_with_a_role_arn_is_the_only_configuration_that_selects_glue``,
    and the "only" is what the glue-first reversal removes: with a role ARN
    present, every value except an exact ``pandas`` or ``shadow`` selects
    ``glue``, including ``None``. The original assertion is kept as the first
    line — ``glue`` asked for by name still works, which is what R2.1 is about.
    """
    assert _mod.resolve_effective_executor("glue", _ROLE) == "glue"
    assert _mod.resolve_effective_executor(None, _ROLE) == "glue"


def test_shadow_survives_the_role_guard():
    """``shadow`` is not downgraded by a missing role ARN.

    Its authoritative half is the pandas proxy (R4.1), which needs no role, so a
    missing ARN must not change the resolved label — otherwise task 10.3 would
    inherit a state where ``shadow`` had already been silently rewritten to
    ``pandas`` and the comparison would never be attempted.
    """
    assert _mod.resolve_effective_executor("shadow", "") == "shadow"
    assert _mod.resolve_effective_executor("shadow", None) == "shadow"


def test_the_role_downgrade_is_reported_at_error_not_warning(caplog):
    """A downgrade from ``glue`` is not a typo and is not logged like one.

    R2.4's WARNING means "outside the vocabulary". ``glue`` with no role ARN is a
    correctly-spelled request the deployment cannot satisfy — a different fact,
    so a different level. At the same level the two are indistinguishable.

    **Unchanged, but no longer the whole story.** Under the old ``pandas``
    fall-through the two lines could not co-occur: an unrecognised value became
    ``pandas`` before the role guard ran. It now becomes ``glue``, so a typo on a
    stack with no role ARN emits BOTH — see
    :func:`test_a_typo_with_no_role_arn_now_emits_both_diagnostics`. This test
    keeps asserting the case it was written for: an exactly-spelled ``glue``
    still produces the ERROR alone.
    """
    with caplog.at_level("DEBUG"):
        assert _mod.resolve_effective_executor("glue", "") == "pandas"
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "VERIFICATION_SESSION_ROLE_ARN" in errors[0].getMessage()
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_a_typo_with_no_role_arn_now_emits_both_diagnostics(caplog):
    """The two log levels stopped being mutually exclusive, and that is new.

    Recorded as a consequence of the glue-first reversal rather than left for
    someone to trip over. Previously an unrecognised value resolved to ``pandas``
    and so could never reach the role guard's ERROR arm; the sibling property
    file asserted that exclusivity outright. Now the same input resolves to
    ``glue``, hits the guard, and produces one line at each level.

    They are still attributable, which is what ``LGR-ad78946-5`` actually
    requires — distinct levels, distinct messages, each naming its own variable —
    so this is a widening of the diagnostic, not a loss of it. Asserted here so a
    future change that collapses either line has to explain itself.
    """
    with caplog.at_level("DEBUG"):
        assert _mod.resolve_effective_executor("gluee", "") == "pandas"

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(warnings) == 1
    assert len(errors) == 1
    assert repr("gluee") in warnings[0]
    assert "VERIFICATION_SESSION_ROLE_ARN" in errors[0]


def test_effective_resolution_is_total_over_the_role_arn():
    """Every combination lands inside the vocabulary.

    A value outside it would become the executor label on the log and the
    report, which no consumer knows how to read.
    """
    for raw in ["pandas", "glue", "shadow", "", None, "nonsense"]:
        for role in ["", None, _ROLE]:
            assert _mod.resolve_effective_executor(raw, role) in _mod.VERIFICATION_EXECUTORS


def test_the_call_site_constructs_the_glue_runner_on_the_glue_branch():
    """The ``glue`` branch imports and constructs ``GlueSessionTestRunner``.

    Asserted against the handler's own source rather than by driving the whole
    handler, because reaching Phase 2 requires a review verdict, a pipeline
    record and a generated-code artifact — machinery this test is not about. The
    weaker source assertion still catches the inert-flag failure: a resolution
    with no consumer.

    **Amended by task 9.3, not deleted.** This asserted the one-line form
    ``GlueSessionTestRunner(region=AWS_REGION, role_arn=session_role_arn)``.
    Adding the third keyword argument (R9.6) put the call across several lines,
    so a single source literal no longer matches anything. What the assertion was
    for — the branch has a real consumer, and the two values it resolves reach it
    — is unchanged and is now pinned per argument, which is also what makes it
    survive the next reformat.
    """
    import inspect

    source = inspect.getsource(_mod)
    assert "from glue_session_runner import GlueSessionTestRunner" in source
    assert "runner = GlueSessionTestRunner(" in source
    assert "region=AWS_REGION" in source
    assert "role_arn=session_role_arn" in source
    # R9.6 — the URI arrives on the CONSTRUCTOR. Task 9.4 pins the same fact
    # structurally (`inspect.signature`); this keeps the *call site* from
    # silently dropping it, which a signature test alone would not notice.
    assert "source_data_uri=source_data_uri" in source


def test_the_glue_branch_is_entered_only_for_glue_and_not_for_shadow():
    """The dispatch tests ``== VERIFICATION_EXECUTOR_GLUE``, so ``shadow`` falls to
    the proxy branch.

    This is the half of shadow mode R4.1 specifies — the pandas proxy is the
    authoritative executor under ``shadow`` too — so it is not a placeholder.
    Task 10.3 adds the second Glue run and the comparison row alongside it; it
    does not change which result the verdict comes from. Asserted here so that a
    later change routing ``shadow`` into the Glue branch has to break a test that
    explains why it must not.

    **Amended by task 10.3, not deleted** (``LGR-60c4985-6``'s house pattern: an
    assertion that encoded a deliberate hold is changed on purpose, and the
    change stays legible as one). This asserted ``"== VERIFICATION_EXECUTOR_SHADOW"
    not in source`` — that no branch dispatched on ``shadow`` at all — with a
    comment saying 10.3 must add the Glue run ALONGSIDE the authoritative pandas
    run rather than instead of it. There is now such a branch, so the same
    obligation is asserted directly instead of by absence: the ``shadow`` branch
    calls ``record_shadow_verification`` and selects no runner, and the executor
    dispatch that DOES select one is still the two-armed
    ``glue``-or-everything-else test.
    """
    import inspect

    source = inspect.getsource(_mod)
    # The dispatch is an equality test against `glue` with an `else`, so exactly
    # one value reaches the Glue runner and the other two share the proxy arm.
    assert "if verification_executor == VERIFICATION_EXECUTOR_GLUE:" in source
    # Exactly one construction site per runner, both inside that dispatch.
    assert source.count("runner = GlueSessionTestRunner(") == 1
    assert source.count("runner = CodeInterpreterTestRunner(region=AWS_REGION)") == 1
    # The `shadow` branch records the comparison and picks no runner: the verdict
    # still comes from the proxy arm above (R4.1).
    assert "if verification_executor == VERIFICATION_EXECUTOR_SHADOW:" in source
    shadow_branch = source.split("if verification_executor == VERIFICATION_EXECUTOR_SHADOW:", 1)[1]
    assert shadow_branch.lstrip().startswith("record_shadow_verification(")


def test_run_test_is_called_with_exactly_three_arguments_on_both_branches():
    """R9.6 — ``run_test``'s arity does not move.

    32 references and five test files depend on it, and task 9.3 wires
    ``source_data_uri`` through the CONSTRUCTOR for exactly this reason. A
    fourth positional argument added here would be a contract break, so the
    call sites are pinned by source.

    **Amended by task 10.3, not deleted.** There were one call site and now there
    are two: the authoritative one in Phase 2, and shadow mode's
    non-authoritative Glue run in ``record_shadow_verification``. Both are
    three-argument, and the second is pinned here as well — the arity fence has
    to cover every caller or it only fences the one that existed when it was
    written.
    """
    import inspect

    source = inspect.getsource(_mod)
    assert source.count("runner.run_test(transform_block, source_schema, target_schema_for_run)") == 1
    assert "run_test(transform_block, source_schema, target_schema_for_run," not in source
    # The shadow half's call, whose third argument is spelled `target_schema`
    # because it is a parameter of `record_shadow_verification` rather than the
    # handler's local alias.
    assert source.count("runner.run_test(transform_block, source_schema, target_schema)") == 1
    assert "run_test(transform_block, source_schema, target_schema," not in source


def test_the_call_site_reads_both_variables_from_the_environment():
    """Both values are read at the call site, not cached at module scope.

    A module-level read would be captured once per container and go stale
    between a configuration change and the next warm invocation.
    """
    import inspect

    source = inspect.getsource(_mod)
    assert "os.environ.get(VERIFICATION_EXECUTOR_ENV)" in source
    assert "resolve_effective_executor(" in source


def test_role_arn_is_read_from_the_environment_and_not_reconstructed():
    """The ARN comes from configuration, never from account + region.

    Reconstructing it would encode a CDK physical name in Python and produce a
    valid-looking ARN for a role that may not exist — a failure that surfaces as
    an opaque AccessDenied inside a Glue session rather than as a configuration
    error at the call site.
    """
    import inspect

    source = inspect.getsource(_mod)
    assert _mod.VERIFICATION_SESSION_ROLE_ARN_ENV == "VERIFICATION_SESSION_ROLE_ARN"
    assert "os.environ.get(VERIFICATION_SESSION_ROLE_ARN_ENV)" in source
    assert "arn:aws:iam::" not in source
