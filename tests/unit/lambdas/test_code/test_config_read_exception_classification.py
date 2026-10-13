# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Exception classification at the Code_Approval_Setting read (p34 task 2.5, design A5).

Three **examples**, not a property: what is asserted here is an exception
*class* on an I/O boundary, and there is no input space to vary — the
classification is a three-way split over a `ClientError` code plus the
missing-record case. A property test over generated codes would restate
`shared/step_errors.py`'s two frozensets rather than pin the boundary.

**What this file guards, and why it is not a duplicate.** The three-way split
itself is already pinned *at the function*: `test_read_path_errors.py:85-118`
and `test_get_pipeline_metadata_characterization.py` both call
`_mod.get_pipeline_metadata(...)` directly. This file pins the **call site** —
`lambda_handler`'s `get_pipeline_metadata(pipeline_id)` at
`lambda/agentic_data_ingestion_test_code/lambda_function.py:1335`, which p34
task 2.4 made the anchor for `resolve_code_approval_setting` (`:1370`). The
distinction matters because the handler wraps its whole body in
`except Exception` (`:2035`) and only re-raises a `StepFailure` unchanged
because of the explicit `isinstance` check at `:2081-2082`. Remove that check,
or add a `try`/`except` around the configuration read, and the function-level
tests all still pass while a classified `TransientStepFailure` reaches Step
Functions as a permanent `StepFailure` — Retry stops firing and a throttle
becomes a dead run.

That is the exact failure `LGR-b2f79ef-13` (**template / ratified**) was
written about: a blanket `except Exception` in
`schema_mapping.get_target_schema` stringified an already-classified
`StepFailure` / `TransientStepFailure` into a bare `ValueError`, so the read
raised the right class and the clause immediately threw it away. Its
`revisit_when` is "nothing" — narrowing a clause that re-labels a classified
exception has no downside to trade against. These three tests are the guard
against such a clause reappearing at *this* call site.

The third example — `get_item` returning no `Item` — is `LGR-b2f79ef-8`
(**template / ratified**): `get_pipeline_metadata` returns `None` rather than
`{}` for a missing record precisely so the handler can raise instead of letting
a missing record present as "no target schema". The handler's `StepFailure` at
`:1343-1349` names the `pipeline_id`, which is the diagnostic p33 R2.8 asks
for.

**R1.6 and R1.7 are satisfied VACUOUSLY, and this file pins that reading — it
does not change it.** Both criteria say the Approval_Policy SHALL evaluate the
Code_Approval_Setting as `true` when the Pipeline_Record cannot be read (R1.6)
or does not exist (R1.7). Neither antecedent can reach the policy: by the time
`resolve_code_approval_setting` runs, an infrastructure error has already been
raised by `raise_for_read_client_error` (`shared/step_errors.py:185-190`) and a
missing record has already been raised by the handler (`:1343-1349`). The step
**fails**, which is strictly more conservative than "require approval": a
failed step goes `Catch` → `RecordFailure` → `releaseLockFailure` →
`PipelineFailed` and can never reach `CreateInfrastructure`. So R1.6/R1.7's
intent — the run does not proceed unreviewed — holds in the stronger
direction. That is design D1's stated resolution, and design §5 risk 5 flags
it: a reviewer who wants the *literal* reading (route to a human rather than
fail the run) is asking to reverse p33 R2.8, which needs its own spec. Each
test below therefore also asserts that **no approval decision is produced**,
mechanically rather than rhetorically — see `_assert_no_approval_decision`.

**Error codes are taken from the classifier's own sets, not from memory**
(see `_TRANSIENT_CODES` / `_NOT_FOUND_CODES` below, bound off
`raise_for_read_client_error.__globals__`), and each test asserts the
membership its expectation depends on. `ThrottlingException` is in
`TRANSIENT_CLIENT_ERROR_CODES` (`shared/step_errors.py:67`).
`AccessDeniedException` is in **neither** set, and that absence — not a
positive entry — is what routes it to the permanent `StepFailure` at
`shared/step_errors.py:190`. Asserting the membership in the test body means
someone adding either code to a set breaks this file rather than silently
inverting it.

**Validates: Requirements 1.6, 1.7, 8.2**

R8.2 as amended is a **relocation**, not a characterization: design D2 deletes
`create_code.determine_approval_requirement`, and its read-error branch
(`create_code:336-339`'s `except Exception: return True`) relocates here. Its
other three branches relocate to Property 1
(`test_code_approval_setting_prop.py`) and its regeneration escalation to
Property 2 (`test_merged_approval_policy_prop.py`).
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Set region/account before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

# Add Lambda dir and shared dir to path (sibling pattern: test_read_path_errors.py)
_test_code_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _test_code_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy Lambda-layer dependencies before import (sibling pattern)
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "code_interpreter_runner": MagicMock(CodeInterpreterTestRunner=MagicMock()),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": MagicMock(),
    "artifact_schema": MagicMock(create_output_data=MagicMock(return_value={})),
    "metrics_facade": MagicMock(PipelineMetrics=None),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod

# ``test_code`` imports ``StepFailure`` but not its transient subclass, so the
# class the transient expectation turns on has to be reached another way. Bound
# off the classifier's own module namespace rather than by
# ``from step_errors import TransientStepFailure``: the ``patch.dict`` above
# restores ``sys.modules`` on exit, so a re-import can yield a SECOND
# ``step_errors`` instance whose ``StepFailure`` is a different class object and
# ``pytest.raises(_mod.StepFailure)`` silently stops matching. This is the exact
# class the production path constructs (pattern copied from
# ``test_get_pipeline_metadata_characterization.py:128``).
_classifier_globals = _mod.raise_for_read_client_error.__globals__
TransientStepFailure = _classifier_globals["TransientStepFailure"]
assert issubclass(TransientStepFailure, _mod.StepFailure)

#: The classifier's OWN sets, read from the module that decides — so the codes
#: below are chosen from `shared/step_errors.py:54` / `:67` rather than recalled.
_TRANSIENT_CODES = _classifier_globals["TRANSIENT_CLIENT_ERROR_CODES"]
_NOT_FOUND_CODES = _classifier_globals["NOT_FOUND_CLIENT_ERROR_CODES"]

#: In `TRANSIENT_CLIENT_ERROR_CODES` — DynamoDB's throttle code.
THROTTLING_CODE = "ThrottlingException"

#: In NEITHER set. That is the whole reason it is permanent: the classifier
#: returns for a not-found code, raises `TransientStepFailure` for a transient
#: one, and falls through to a permanent `StepFailure` for everything else
#: (`shared/step_errors.py:185-190`).
ACCESS_DENIED_CODE = "AccessDeniedException"

PIPELINE_ID = "pipe-p34-config-read"
RUN_ID = "run-p34-config-read"

#: The three fields the Approval_Policy publishes (design D3). None of them may
#: appear anywhere on a run that failed at the configuration read.
_APPROVAL_FIELDS = ("approval_required", "quality_gate_requires_approval", "approval_decision_inputs")


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, "GetItem")


def _dynamodb_whose_pipelines_table(*, raises=None, returns=None) -> MagicMock:
    """A ``dynamodb`` resource stand-in for the handler's pipelines read.

    ``get_pipeline_metadata`` (`:161`) resolves its table through
    ``dynamodb.Table(...)`` on every call, so patching the module's ``dynamodb``
    attribute is what reaches it.
    """
    table = MagicMock()
    if raises is not None:
        table.get_item.side_effect = raises
    else:
        table.get_item.return_value = returns
    dynamodb = MagicMock()
    dynamodb.Table.return_value = table
    return dynamodb


class _HandlerRun:
    """What one handler invocation did, for the assertions below."""

    def __init__(self, exception, runs_table, resolve_setting, merge_policy, quality_gate):
        self.exception = exception
        self.runs_table = runs_table
        self.resolve_setting = resolve_setting
        self.merge_policy = merge_policy
        self.quality_gate = quality_gate
        self.returned = None  # set only if the handler returned, which it must not


def _run_handler(dynamodb: MagicMock) -> _HandlerRun:
    """Invoke ``lambda_handler`` with the pipelines read wired to ``dynamodb``.

    The three approval functions are patched with bare mocks so "no approval
    decision is produced" can be asserted as *not called* rather than inferred
    from an absent field. If control ever reached one of them the assertion
    below fails; nothing downstream depends on their return values here,
    because every case in this file raises before them.
    """
    runs_table = MagicMock()
    resolve_setting = MagicMock(name="resolve_code_approval_setting")
    merge_policy = MagicMock(name="determine_merged_approval_requirement")
    quality_gate = MagicMock(name="determine_code_approval_requirement")

    event = {"run_id": RUN_ID, "pipeline_id": PIPELINE_ID}
    run = _HandlerRun(None, runs_table, resolve_setting, merge_policy, quality_gate)

    with (
        patch.object(_mod, "dynamodb", dynamodb),
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "resolve_code_approval_setting", resolve_setting),
        patch.object(_mod, "determine_merged_approval_requirement", merge_policy),
        patch.object(_mod, "determine_code_approval_requirement", quality_gate),
        patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}),
        pytest.raises(_mod.StepFailure) as exc_info,
    ):
        run.returned = _mod.lambda_handler(event, None)

    run.exception = exc_info.value
    return run


def _assert_no_approval_decision(run: _HandlerRun) -> None:
    """The mechanical half of R1.6/R1.7's vacuous satisfaction (design D1).

    "No approval decision is produced" is asserted four ways, none of them
    rhetorical:

    1. The setting resolver was never called, so no Code_Approval_Setting was
       evaluated at all.
    2. The Approval_Policy and the Code_Quality_Gate were never called, so no
       merged decision and no unmerged verdict exist.
    3. The handler returned nothing — it raised, so there is no
       ``$.test_code_result`` for ``CheckCodeApproval`` to read. Combined with
       (1)-(2) this is what makes failing strictly more conservative than
       "require approval".
    4. The one state row the failure path DOES write (a ``status: "failed"``
       row) carries none of the three approval fields. The row is asserted
       rather than forbidden, because forbidding it would be false: the handler
       records its own failure on purpose (P28 R1.1).

    **AMENDED, not deleted** (``LGR-60c4985-6``, citing ``LGR-5906ec6-2``: this
    repo encodes a deliberate shape as a test assertion, so changing the shape
    means changing the test on purpose). Assertion 4 used to read
    ``runs_table.put_item.call_args_list``, because the failure path wrote its
    row with a full-row ``PutItem``. That was a latent clobber of the same class
    ``build_state_row_update`` documents: ``PutItem`` replaces the whole item, so
    on a second ``test_code`` invocation — routine under regeneration — the
    failure write reduced a row an earlier successful invocation had filled in to
    just its own attributes. The failure path is now a SET update through
    ``build_state_row_update``, so the row is rebuilt from the expression values
    (the house pattern, ``test_report_names_the_running_executor.py:163``). What
    is asserted is unchanged: the approval fields are absent, and a
    ``status: "failed"`` row IS written.
    """
    run.resolve_setting.assert_not_called()
    run.merge_policy.assert_not_called()
    run.quality_gate.assert_not_called()
    assert run.returned is None

    written_items = []
    for call in run.runs_table.update_item.call_args_list:
        values = {name.lstrip(":"): value for name, value in call.kwargs["ExpressionAttributeValues"].items()}
        if "step_sequence" not in values:  # not a state-row write
            continue
        written_items.append({**call.kwargs["Key"], **values})

    for item in written_items:
        for field in _APPROVAL_FIELDS:
            assert field not in item, f"failure-path state row must not carry {field!r}"
        # `next_step` is derived from the merged field, so it cannot exist
        # either — the Code_Approval_Gate's sibling signal must be absent too.
        assert "next_step" not in item
    # And the row that IS written says what happened, so the absence above is
    # not the absence of a write.
    assert [item.get("status") for item in written_items] == ["failed"]
    # The mechanism, not just the content: a full-row PutItem on this shared row
    # is what the conversion removed, so its return is a failure here.
    run.runs_table.put_item.assert_not_called()


def test_a_throttled_pipeline_read_raises_transient_step_failure():
    """R1.6, transient half. Retry fires; no approval decision is produced.

    ``TransientStepFailure`` is the class name Step Functions matches
    ``ErrorEquals`` against, and ``CONTAINER_STEP_RETRY``
    (`orchestration-stack.ts`) retries it three times with backoff. A
    re-labelled exception loses that retry — which is why the CLASS, not just
    "it raised", is the assertion.
    """
    assert THROTTLING_CODE in _TRANSIENT_CODES  # the expectation's premise, from the classifier's own set

    run = _run_handler(_dynamodb_whose_pipelines_table(raises=_client_error(THROTTLING_CODE)))
    exc = run.exception

    # `type(...) is` rather than isinstance: a re-wrap into the parent class
    # would satisfy isinstance and lose the retry, which is the whole defect
    # `LGR-b2f79ef-13` records.
    assert type(exc).__name__ == "TransientStepFailure"
    assert isinstance(exc, TransientStepFailure)
    assert exc.error_type == "transient"
    assert exc.step_name == "test_code"
    assert exc.details["operation"] == "get_pipeline_metadata"
    assert exc.details["error_code"] == THROTTLING_CODE
    # The ClientError is still the cause. A `except Exception` at the call site
    # would make the cause the TransientStepFailure and the class StepFailure.
    assert isinstance(exc.__cause__, ClientError)

    _assert_no_approval_decision(run)


def test_an_access_denied_pipeline_read_raises_a_permanent_step_failure():
    """R1.6, permanent half. No retry, no approval decision.

    Permanent is correct: an IAM denial cannot start succeeding on attempt two,
    and ``stepRetry`` matches ``TransientStepFailure`` and Lambda service
    errors only. The classification comes from `AccessDeniedException` being in
    neither of the classifier's sets, so both memberships are asserted.
    """
    assert ACCESS_DENIED_CODE not in _TRANSIENT_CODES
    assert ACCESS_DENIED_CODE not in _NOT_FOUND_CODES

    run = _run_handler(_dynamodb_whose_pipelines_table(raises=_client_error(ACCESS_DENIED_CODE)))
    exc = run.exception

    assert type(exc).__name__ == "StepFailure"
    assert not isinstance(exc, TransientStepFailure)
    assert exc.error_type == "permanent"
    assert exc.step_name == "test_code"
    assert exc.details["operation"] == "get_pipeline_metadata"
    assert exc.details["error_code"] == ACCESS_DENIED_CODE
    assert isinstance(exc.__cause__, ClientError)

    _assert_no_approval_decision(run)


def test_a_missing_pipeline_record_raises_step_failure_naming_the_pipeline_id():
    """R1.7. `get_item` succeeds and returns no `Item` — no record exists.

    `LGR-b2f79ef-8` (**template / ratified**) is why this arrives as a raise
    rather than as `{}`: `get_pipeline_metadata` returns `None` for a missing
    record specifically so "no pipeline record" cannot collapse into "the
    record exists and carries no target schema". The handler turns that `None`
    into a `StepFailure` naming the `pipeline_id` (`:1343-1349`), which is the
    diagnostic p33 R2.8 asks for — so the id is asserted in both the message
    and the details, not just that something raised.
    """
    run = _run_handler(_dynamodb_whose_pipelines_table(returns={}))
    exc = run.exception

    assert type(exc).__name__ == "StepFailure"
    assert exc.error_type == "permanent"
    assert exc.step_name == "test_code"
    assert PIPELINE_ID in exc.message
    assert exc.details["pipeline_id"] == PIPELINE_ID
    assert exc.details["operation"] == "get_pipeline_metadata"
    # No ClientError was involved: this path never entered the `except`.
    assert exc.__cause__ is None

    _assert_no_approval_decision(run)
