// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The JDBC schema-detection polls must survive a backgrounded tab.
 *
 * WHY THIS EXISTS AS A TEST RATHER THAN A COMMENT
 * -----------------------------------------------
 * `refetchIntervalInBackground: false` is the defensible-looking default, and it is
 * what these two hooks shipped with: don't hammer the API when nobody is watching.
 * It is wrong HERE, and only here, because of what it is paired with — the caller's
 * deadline (`JdbcSourceDetectionPanel.tsx`, `DETECTION_TIMEOUT_MS`) is a plain
 * `setTimeout` on WALL CLOCK. Pause the observation while the clock keeps running
 * and the deadline fires on a detection it never looked at.
 *
 * That is not hypothetical. On a real detection the poll issued exactly 60 requests
 * over 195s, every one returning RUNNING, and then stopped dead with nothing in
 * `use-resources.ts` able to stop it there. The crawl SUCCEEDED about a minute
 * later; polling the same crawler afterwards returned READY with the full 5-column
 * schema. The wizard had already told the user schema detection "did not complete
 * within 12 minutes" — a claim about the crawler that the code never checked.
 *
 * The window is far too tight to leave focus-gated. A JDBC discovery crawl does
 * ~2-3 min of work (median runtime 165s) and Glue holds `State: RUNNING` well past
 * that through provisioning and teardown, which is why the caller's cap is 12
 * minutes rather than 5. Glancing away during a 5-minute wait is ordinary, not an
 * edge case.
 *
 * WHAT THIS CANNOT CATCH, stated so it does not overclaim: it asserts the option
 * these hooks pass, not that React Query honours it, and not that the caller's
 * deadline is still a wall-clock timer. If someone makes the deadline pause on blur
 * too, background polling stops being load-bearing and these assertions become
 * merely harmless — they would not fail to tell you so. The pairing is what matters,
 * and it is carried by the comments on both sides.
 *
 * The OTHER hooks that set this false are correct: list and metric refreshes have no
 * completion race and no paired deadline. This is a rule about these two, not a
 * project-wide preference — which is why it is asserted per hook rather than by
 * sweeping the directory.
 */
import { describe, it, expect, vi } from 'vitest';

const mockUseQuery = vi.fn(() => ({ data: undefined }));
vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: unknown) => mockUseQuery(options as never),
  useMutation: (options: unknown) => mockUseQuery(options as never),
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));
vi.mock('../../services/api-client', () => ({ apiClient: { get: vi.fn(), post: vi.fn() } }));

const { useJdbcDetectSchemaStatus, useJdbcDetectTargetSchemaStatus } = await import('../use-resources');

interface CapturedOptions {
  enabled: boolean;
  refetchInterval: unknown;
  refetchIntervalInBackground: boolean;
}

/** The options the hook hands React Query, for a started detection. */
function sourceOptions(): CapturedOptions {
  mockUseQuery.mockClear();
  useJdbcDetectSchemaStatus('agentic-data-ingestion-discover-a51612d7361d');
  return mockUseQuery.mock.calls[0][0] as unknown as CapturedOptions;
}

function targetOptions(): CapturedOptions {
  mockUseQuery.mockClear();
  useJdbcDetectTargetSchemaStatus('agentic-data-ingestion-discover-a51612d7361d');
  return mockUseQuery.mock.calls[0][0] as unknown as CapturedOptions;
}

/** Drive the `refetchInterval` callback the way React Query does. */
function intervalFor(options: CapturedOptions, data: unknown): unknown {
  const cb = options.refetchInterval as (q: { state: { data: unknown } }) => unknown;
  return cb({ state: { data } });
}

describe.each([
  ['source', sourceOptions],
  ['target', targetOptions],
])('useJdbcDetect%sSchemaStatus — polling survives a blurred tab', (_label, optionsFor) => {
  it('polls in the background, because the caller times out on wall clock', () => {
    expect(optionsFor().refetchIntervalInBackground).toBe(true);
  });

  it('keeps polling while the crawler is RUNNING', () => {
    const options = optionsFor();
    const interval = intervalFor(options, { status: 'RUNNING' });
    expect(typeof interval).toBe('number');
    expect(interval).toBeGreaterThan(0);
  });

  it('polls before the first response, so a slow first GET does not end the poll', () => {
    const interval = intervalFor(optionsFor(), undefined);
    expect(typeof interval).toBe('number');
    expect(interval).toBeGreaterThan(0);
  });

  it.each(['READY', 'FAILED'])('stops on the terminal status %s', (status) => {
    // The counterpart to the assertions above: background polling must not mean
    // polling forever. These two are what actually end it.
    expect(intervalFor(optionsFor(), { status })).toBe(false);
  });

  it('is idle until a crawler name exists', () => {
    mockUseQuery.mockClear();
    useJdbcDetectSchemaStatus(null);
    const options = mockUseQuery.mock.calls[0][0] as unknown as CapturedOptions;
    expect(options.enabled).toBe(false);
  });
});
