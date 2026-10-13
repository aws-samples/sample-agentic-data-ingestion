// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { previewKeys, notificationKeys, updateKeys } from '../hooks/query-keys';

/**
 * Feature: p12c-addon, Property 2: Query key factories embed parameters in tuples
 *
 * For any set of input parameters (pipelineId, runId, limit, unreadOnly),
 * the corresponding query key factory (previewKeys, notificationKeys, updateKeys)
 * shall return a readonly tuple whose elements include all provided parameters,
 * ensuring distinct cache entries for distinct inputs.
 *
 * Validates: Requirements 1.4
 */
describe('Property 2: Query key factories embed parameters in tuples', () => {
  const nonEmptyString = fc.string({ minLength: 1 });

  it('previewKeys.pipeline embeds pipelineId', () => {
    fc.assert(
      fc.property(nonEmptyString, (pipelineId) => {
        const key = previewKeys.pipeline(pipelineId);
        expect(key[0]).toBe('preview');
        expect(key[1]).toBe(pipelineId);
        expect(key).toHaveLength(2);
      }),
      { numRuns: 100 },
    );
  });

  it('previewKeys.run embeds pipelineId and runId', () => {
    fc.assert(
      fc.property(nonEmptyString, nonEmptyString, (pipelineId, runId) => {
        const key = previewKeys.run(pipelineId, runId);
        expect(key[0]).toBe('preview');
        expect(key[1]).toBe(pipelineId);
        expect(key[2]).toBe(runId);
        expect(key).toHaveLength(3);
      }),
      { numRuns: 100 },
    );
  });

  it('notificationKeys.list embeds params object', () => {
    fc.assert(
      fc.property(
        fc.option(fc.nat(), { nil: undefined }),
        fc.option(fc.boolean(), { nil: undefined }),
        (limit, unreadOnly) => {
          const params = { limit, unreadOnly };
          const key = notificationKeys.list(params);
          expect(key[0]).toBe('notifications');
          expect(key[1]).toBe('list');
          expect(key[2]).toEqual(params);
          expect(key).toHaveLength(3);
        },
      ),
      { numRuns: 100 },
    );
  });

  it('updateKeys.list embeds limit', () => {
    fc.assert(
      fc.property(fc.option(fc.nat(), { nil: undefined }), (limit) => {
        const key = updateKeys.list(limit);
        expect(key[0]).toBe('updates');
        expect(key[1]).toBe('list');
        expect(key[2]).toBe(limit);
        expect(key).toHaveLength(3);
      }),
      { numRuns: 100 },
    );
  });

  it('distinct inputs produce distinct keys for previewKeys', () => {
    fc.assert(
      fc.property(
        nonEmptyString,
        nonEmptyString.filter((s2) => s2.length > 0),
        (id1, id2) => {
          fc.pre(id1 !== id2);
          const key1 = previewKeys.pipeline(id1);
          const key2 = previewKeys.pipeline(id2);
          expect(key1).not.toEqual(key2);
        },
      ),
      { numRuns: 100 },
    );
  });

  it('distinct inputs produce distinct keys for updateKeys', () => {
    fc.assert(
      fc.property(fc.nat(), fc.nat(), (limit1, limit2) => {
        fc.pre(limit1 !== limit2);
        const key1 = updateKeys.list(limit1);
        const key2 = updateKeys.list(limit2);
        expect(key1).not.toEqual(key2);
      }),
      { numRuns: 100 },
    );
  });
});
