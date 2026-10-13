// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * How the dashboard reads the two cancellation statuses.
 *
 * `CANCELLING`: the run's Glue job is being stopped. It is still in flight, so
 * views keep polling until it becomes `CANCELLED`, and it renders as in
 * progress. `CANCELLED` is final, and a step that was running when the run was
 * cancelled did not succeed.
 */
import { describe, it, expect } from 'vitest';
import {
  getStatusIcon,
  isActiveRunStatus,
  isTerminalStatus,
  resolveStepStatus,
  statusType,
} from '../formatting';

describe('CANCELLING', () => {
  it('keeps a run view polling', () => {
    expect(isActiveRunStatus('CANCELLING')).toBe(true);
    expect(isTerminalStatus('CANCELLING')).toBe(false);
  });

  it('renders as in progress with its own icon', () => {
    expect(statusType('CANCELLING')).toBe('in-progress');
    expect(getStatusIcon('CANCELLING')).not.toBe(getStatusIcon('UNKNOWN_STATUS'));
  });
});

describe('CANCELLED', () => {
  it('is final', () => {
    expect(isTerminalStatus('CANCELLED')).toBe(true);
    expect(isActiveRunStatus('CANCELLED')).toBe(false);
  });

  it('does not turn a step that was running into a success', () => {
    expect(resolveStepStatus('running', 'CANCELLED')).toBe('cancelled');
    expect(getStatusIcon(resolveStepStatus('running', 'CANCELLED'))).not.toBe(getStatusIcon('success'));
  });

  it('renders as an error, as it did before', () => {
    expect(statusType('CANCELLED')).toBe('error');
  });
});

describe('isActiveRunStatus', () => {
  it.each(['RUNNING', 'IN_PROGRESS', 'WAITING_APPROVAL', 'CANCELLING'])('%s is active', (status) => {
    expect(isActiveRunStatus(status)).toBe(true);
  });

  it.each(['SUCCEEDED', 'FAILED', 'SKIPPED', 'CANCELLED', '', undefined, null])('%s is not', (status) => {
    expect(isActiveRunStatus(status)).toBe(false);
  });
});
