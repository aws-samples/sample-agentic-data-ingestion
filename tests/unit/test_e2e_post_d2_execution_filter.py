# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the e2e subject filter in the test_failure remediation route.

`tests/e2e/approvals/test_test_failure_remediation_route.py::TestTwoAttemptsOneGate`
asserts a property of the definition P32 D2 deploys (two automatic CreateCode
attempts, then one forced-review gate). It can only assert that against an
execution that RAN under that definition. The original version tried to
approximate "post-deploy" with `break  # newest page only`, and against a
freshly-deployed account that produced a false failure: the single execution in
the whole account that ever reached `ForceTestReviewApproval` started
2026-08-04T12:28Z, a day BEFORE the loop-back edge deployed at
2026-08-05T12:04Z, and it sits on the newest page.

`post_d2_gate_candidates` replaced that with a self-selecting filter — an
execution qualifies only if its own history contains `CheckRegenerationAttempt`,
a state that exists solely in the post-D2 definition. The e2e layer itself is
skipped unless `AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true`, so this file is where
that filter is actually exercised: with a stubbed Step Functions client, no AWS,
in `make test-fast`.

`test_pre_d2_gated_execution_is_not_a_subject` is the regression case. It
reproduces the live counterexample and fails on the "newest page only" logic.

These tests use NO real AWS resources — the Step Functions client is a stub.

Validates: Requirements 2.17 (subject selection for the deployed assertion)
"""

from __future__ import annotations

from typing import Any

from tests.e2e.approvals.test_test_failure_remediation_route import (
    FORCED_REVIEW_STATE,
    POST_D2_MARKER_STATE,
    post_d2_gate_candidates,
)

STATE_MACHINE_ARN = "arn:aws:states:us-west-2:123456789012:stateMachine:agentic-data-ingestion-pipeline"

#: The live counterexample: one CreateCode, one gate, and no D2 marker because
#: the definition it ran under had none.
PRE_D2_GATED_HISTORY = [
    "FileValidation",
    "SchemaExtract",
    "SchemaCompare",
    "SchemaMapping",
    "CreateCode",
    "TestCode",
    "TestResultChoice",
    FORCED_REVIEW_STATE,
]

#: A post-D2 run that regenerated once and then reached the gate.
POST_D2_GATED_HISTORY = [
    "FileValidation",
    "SchemaExtract",
    POST_D2_MARKER_STATE,
    "ApplyDefaultRegenerationAttempt",
    "CreateCode",
    "TestCode",
    "TestResultChoice",
    "ComputeNextRegenerationAttempt",
    "IncrementRegenerationAttempt",
    "CreateCode",
    "TestCode",
    "TestResultChoice",
    FORCED_REVIEW_STATE,
]

#: A post-D2 run whose test passed — no gate, so not a subject either.
POST_D2_PASSING_HISTORY = [
    "FileValidation",
    POST_D2_MARKER_STATE,
    "CreateCode",
    "TestCode",
    "TestResultChoice",
    "CheckCodeApproval",
    "RunPipeline",
]


class _FakePaginator:
    """Minimal stand-in for a botocore paginator over pre-baked pages."""

    def __init__(self, pages_for: Any) -> None:
        self._pages_for = pages_for

    def paginate(self, **kwargs: Any) -> Any:
        pages = self._pages_for(kwargs)
        max_items = (kwargs.get("PaginationConfig") or {}).get("MaxItems")
        if max_items is None:
            yield from pages
            return
        # Mirror botocore: MaxItems bounds the total items yielded, not pages.
        emitted = 0
        for page in pages:
            items_key = "executions" if "executions" in page else "events"
            remaining = max_items - emitted
            if remaining <= 0:
                return
            items = page[items_key][:remaining]
            emitted += len(items)
            yield {**page, items_key: items}


class _FakeStepFunctions:
    """Stub client serving list_executions and get_execution_history pages.

    Args:
        histories: ordered mapping of execution arn -> list of history PAGES,
            each page a list of entered state names. Multiple pages exercise the
            history pagination the filter depends on.
        execution_pages: how the arns are split across list_executions pages.
    """

    def __init__(
        self,
        histories: dict[str, list[list[str]]],
        execution_pages: list[list[str]] | None = None,
    ) -> None:
        self._histories = histories
        self._execution_pages = execution_pages or [list(histories)]
        self.history_calls: list[str] = []

    def get_paginator(self, operation: str) -> _FakePaginator:
        if operation == "list_executions":
            return _FakePaginator(self._list_execution_pages)
        if operation == "get_execution_history":
            return _FakePaginator(self._history_pages)
        raise AssertionError(f"unexpected paginator: {operation}")

    def _list_execution_pages(self, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        assert kwargs["stateMachineArn"] == STATE_MACHINE_ARN
        return [{"executions": [{"executionArn": arn} for arn in page]} for page in self._execution_pages]

    def _history_pages(self, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        arn = kwargs["executionArn"]
        self.history_calls.append(arn)
        return [{"events": [_state_entered_event(name) for name in page]} for page in self._histories.get(arn, [[]])]


def _state_entered_event(name: str) -> dict[str, Any]:
    """A history event in the shape the filter reads."""
    return {"type": "TaskStateEntered", "stateEnteredEventDetails": {"name": name}}


class TestSubjectSelection:
    """Only executions that ran under the post-D2 definition are subjects."""

    def test_pre_d2_gated_execution_is_not_a_subject(self):
        """The live counterexample: a gated run that predates the edge.

        This is the regression case. Under the old "newest page only" filter
        this execution WAS the subject and its single CreateCode entry failed
        the `>= 2` assertion — reporting an unobserved property as a broken one.
        """
        client = _FakeStepFunctions({"arn:pre-d2": [PRE_D2_GATED_HISTORY]})

        examined, post_d2, gated = post_d2_gate_candidates(client, STATE_MACHINE_ARN)

        assert examined == ["arn:pre-d2"]
        assert post_d2 == [], "an execution without the D2 marker state cannot exhibit 2.17"
        assert gated == [], "a pre-D2 execution must not be asserted against"

    def test_post_d2_gated_execution_is_a_subject_with_its_entered_states(self):
        client = _FakeStepFunctions({"arn:post-d2": [POST_D2_GATED_HISTORY]})

        examined, post_d2, gated = post_d2_gate_candidates(client, STATE_MACHINE_ARN)

        assert examined == ["arn:post-d2"]
        assert post_d2 == ["arn:post-d2"]
        assert len(gated) == 1
        arn, entered = gated[0]
        assert arn == "arn:post-d2"
        # The counts the e2e assertions are built on.
        assert entered.count("CreateCode") == 2
        assert entered.count(FORCED_REVIEW_STATE) == 1

    def test_post_d2_execution_that_never_gated_is_not_in_gated(self):
        """A passing run is post-D2 but is not evidence about the gate."""
        client = _FakeStepFunctions({"arn:passing": [POST_D2_PASSING_HISTORY]})

        examined, post_d2, gated = post_d2_gate_candidates(client, STATE_MACHINE_ARN)

        assert examined == ["arn:passing"]
        assert post_d2 == ["arn:passing"], "it ran the post-D2 definition, which the skip message counts"
        assert gated == []

    def test_mixed_account_selects_only_the_post_d2_gated_run(self):
        """The deployed shape: many pre-D2 runs, one post-D2 gated run.

        The pre-D2 gated execution is deliberately on the FIRST page and the
        post-D2 one on the second, which is the ordering that defeated the
        newest-page filter.
        """
        client = _FakeStepFunctions(
            histories={
                "arn:pre-d2": [PRE_D2_GATED_HISTORY],
                "arn:passing": [POST_D2_PASSING_HISTORY],
                "arn:post-d2": [POST_D2_GATED_HISTORY],
            },
            execution_pages=[["arn:pre-d2", "arn:passing"], ["arn:post-d2"]],
        )

        examined, post_d2, gated = post_d2_gate_candidates(client, STATE_MACHINE_ARN)

        assert examined == ["arn:pre-d2", "arn:passing", "arn:post-d2"]
        assert post_d2 == ["arn:passing", "arn:post-d2"]
        assert [arn for arn, _ in gated] == ["arn:post-d2"]


class TestHistoryPagination:
    """The history must be read whole — a truncated one undercounts CreateCode."""

    def test_states_on_a_later_history_page_are_counted(self):
        """The second CreateCode lives on page 2 and must still be counted.

        A single `get_execution_history` page caps at 1000 events; a run that
        regenerates emits more than that. Reading only the first page would
        report one attempt and fail the `>= 2` assertion — the same false
        failure by a different route.
        """
        first_page = POST_D2_GATED_HISTORY[:6]
        second_page = POST_D2_GATED_HISTORY[6:]
        client = _FakeStepFunctions({"arn:long": [first_page, second_page]})

        _, _, gated = post_d2_gate_candidates(client, STATE_MACHINE_ARN)

        assert len(gated) == 1
        _, entered = gated[0]
        assert entered == POST_D2_GATED_HISTORY
        assert entered.count("CreateCode") == 2
        assert entered.count(FORCED_REVIEW_STATE) == 1


class TestCostBound:
    """The execution cap bounds the API spend without deciding correctness."""

    def test_max_executions_bounds_the_history_reads(self):
        histories = {f"arn:{index}": [POST_D2_PASSING_HISTORY] for index in range(10)}
        client = _FakeStepFunctions(histories)

        examined, _, _ = post_d2_gate_candidates(client, STATE_MACHINE_ARN, max_executions=4)

        assert len(examined) == 4
        # One history read per examined execution, and no more.
        assert len(client.history_calls) == 4
