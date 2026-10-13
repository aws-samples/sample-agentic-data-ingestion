# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Concurrent trigger integration test.

Two trigger invocations for same pipeline simultaneously → verify one
succeeds, other deferred.

Requires threading + SFN mocking — future work.

Validates: Requirements 17.5
"""

import pytest


@pytest.mark.skip(reason="Requires threading + SFN mocking for concurrent invocation — future work")
def test_concurrent_triggers_one_succeeds_one_deferred():
    """Two simultaneous triggers for same pipeline → one succeeds, one deferred.

    Full flow:
    1. Set up pipeline with no running executions
    2. Launch two threads, each invoking pipeline_trigger
    3. Verify exactly one SFN execution started
    4. Verify the other invocation raised (deferred to SQS retry)
    """
    pass
