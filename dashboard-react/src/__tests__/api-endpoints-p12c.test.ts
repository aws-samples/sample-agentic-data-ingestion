// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { API_ENDPOINTS } from '../services/api-endpoints';

// ── Unit tests for new endpoint paths (Task 1.3) ────────────────

describe('API Endpoints — P12c Addon', () => {
  describe('preview endpoints', () => {
    it('latestRun returns correct path', () => {
      expect(API_ENDPOINTS.preview.latestRun('pipe-1')).toBe(
        '/pipelines/pipe-1/preview',
      );
    });

    it('specificRun returns correct path', () => {
      expect(API_ENDPOINTS.preview.specificRun('pipe-1', 'run-1')).toBe(
        '/pipelines/pipe-1/run/run-1/preview',
      );
    });

    it('latestRun encodes special characters', () => {
      expect(API_ENDPOINTS.preview.latestRun('a b')).toBe(
        '/pipelines/a%20b/preview',
      );
    });

    it('specificRun encodes special characters in both IDs', () => {
      expect(API_ENDPOINTS.preview.specificRun('a/b', 'c d')).toBe(
        '/pipelines/a%2Fb/run/c%20d/preview',
      );
    });
  });

  describe('notifications endpoints', () => {
    it('list is a static path', () => {
      expect(API_ENDPOINTS.notifications.list).toBe('/notifications');
    });

    it('markRead returns correct path', () => {
      expect(API_ENDPOINTS.notifications.markRead('notif-1')).toBe(
        '/notifications/notif-1/read',
      );
    });

    it('markRead encodes special characters', () => {
      expect(API_ENDPOINTS.notifications.markRead('a/b')).toBe(
        '/notifications/a%2Fb/read',
      );
    });
  });

  describe('updates endpoints', () => {
    it('list is a static path', () => {
      expect(API_ENDPOINTS.updates.list).toBe('/updates');
    });
  });

  // ── Property test for URL construction (Task 1.4) ───────────────
  /**
   * Feature: p12c-addon, Property 1: Endpoint URL construction embeds encoded IDs
   *
   * For any non-empty string used as a pipeline ID, run ID, or notification ID,
   * the corresponding API_ENDPOINTS function shall produce a URL string that
   * contains the encodeURIComponent-encoded ID at the correct path segment,
   * and the URL shall not contain any unencoded special characters from the input.
   *
   * Validates: Requirements 1.1, 1.2
   */
  describe('Property 1: Endpoint URL construction embeds encoded IDs', () => {
    const nonEmptyString = fc.string({ minLength: 1 });

    it('preview.latestRun embeds encoded pipelineId', () => {
      fc.assert(
        fc.property(nonEmptyString, (pipelineId) => {
          const url = API_ENDPOINTS.preview.latestRun(pipelineId);
          const encoded = encodeURIComponent(pipelineId);
          expect(url).toBe(`/pipelines/${encoded}/preview`);
          expect(url).toContain(encoded);
        }),
        { numRuns: 100 },
      );
    });

    it('preview.specificRun embeds encoded pipelineId and runId', () => {
      fc.assert(
        fc.property(nonEmptyString, nonEmptyString, (pipelineId, runId) => {
          const url = API_ENDPOINTS.preview.specificRun(pipelineId, runId);
          const encPipeline = encodeURIComponent(pipelineId);
          const encRun = encodeURIComponent(runId);
          expect(url).toBe(`/pipelines/${encPipeline}/run/${encRun}/preview`);
          expect(url).toContain(encPipeline);
          expect(url).toContain(encRun);
        }),
        { numRuns: 100 },
      );
    });

    it('notifications.markRead embeds encoded notificationId', () => {
      fc.assert(
        fc.property(nonEmptyString, (notificationId) => {
          const url = API_ENDPOINTS.notifications.markRead(notificationId);
          const encoded = encodeURIComponent(notificationId);
          expect(url).toBe(`/notifications/${encoded}/read`);
          expect(url).toContain(encoded);
        }),
        { numRuns: 100 },
      );
    });
  });
});
