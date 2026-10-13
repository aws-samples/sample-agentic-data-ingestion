// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import { API_ENDPOINTS } from '../services/api-endpoints';

describe('API Endpoints — P17 JDBC Schema Detection', () => {
  it('jdbcDetectSchema is the static POST path', () => {
    expect(API_ENDPOINTS.resources.jdbcDetectSchema).toBe(
      '/resources/jdbc/detect-schema',
    );
  });

  it('jdbcDetectSchemaStatus is a function that builds the GET path with the crawler name', () => {
    expect(API_ENDPOINTS.resources.jdbcDetectSchemaStatus('agentic-data-ingestion-discover-abc123')).toBe(
      '/resources/jdbc/detect-schema/agentic-data-ingestion-discover-abc123',
    );
  });

  it('jdbcDetectSchemaStatus URL-encodes special characters in the crawler name', () => {
    // Crawler names from AWS shouldn't contain reserved chars but we still test the encoding
    // contract because the convention is `enc(...)` everywhere else.
    expect(API_ENDPOINTS.resources.jdbcDetectSchemaStatus('name with space')).toBe(
      '/resources/jdbc/detect-schema/name%20with%20space',
    );
    expect(API_ENDPOINTS.resources.jdbcDetectSchemaStatus('a/b')).toBe(
      '/resources/jdbc/detect-schema/a%2Fb',
    );
  });

  it('all JDBC endpoints live under /resources/jdbc/', () => {
    expect(API_ENDPOINTS.resources.jdbcDetectSchema).toMatch(/^\/resources\/jdbc\//);
    expect(API_ENDPOINTS.resources.jdbcDetectSchemaStatus('x')).toMatch(/^\/resources\/jdbc\//);
  });
});
