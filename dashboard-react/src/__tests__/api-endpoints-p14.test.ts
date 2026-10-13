// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import { API_ENDPOINTS } from '../services/api-endpoints';

describe('API Endpoints — P14 RDS Database Targets', () => {
  it('rdsInstances is a static path', () => {
    expect(API_ENDPOINTS.resources.rdsInstances).toBe('/resources/rds/instances');
  });

  it('rdsClusters is a static path', () => {
    expect(API_ENDPOINTS.resources.rdsClusters).toBe('/resources/rds/clusters');
  });

  it('rdsGlueConnections is a static path', () => {
    expect(API_ENDPOINTS.resources.rdsGlueConnections).toBe('/resources/rds/glue-connections');
  });

  it('rdsSecrets is a static path', () => {
    expect(API_ENDPOINTS.resources.rdsSecrets).toBe('/resources/rds/secrets');
  });

  it('all RDS endpoints start with /resources/rds/', () => {
    const rdsEndpoints = [
      API_ENDPOINTS.resources.rdsInstances,
      API_ENDPOINTS.resources.rdsClusters,
      API_ENDPOINTS.resources.rdsGlueConnections,
      API_ENDPOINTS.resources.rdsSecrets,
    ];
    for (const ep of rdsEndpoints) {
      expect(ep).toMatch(/^\/resources\/rds\//);
    }
  });
});
