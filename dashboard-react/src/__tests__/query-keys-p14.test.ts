// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import { resourceKeys } from '../hooks/query-keys';

describe('Query Keys — P14 RDS Database Targets', () => {
  it('rdsInstances returns correct key tuple', () => {
    const key = resourceKeys.rdsInstances();
    expect(key).toEqual(['resources', 'rds', 'instances']);
  });

  it('rdsClusters returns correct key tuple', () => {
    const key = resourceKeys.rdsClusters();
    expect(key).toEqual(['resources', 'rds', 'clusters']);
  });

  it('rdsGlueConnections returns correct key tuple', () => {
    const key = resourceKeys.rdsGlueConnections();
    expect(key).toEqual(['resources', 'rds', 'glue-connections']);
  });

  it('rdsSecrets returns correct key tuple', () => {
    const key = resourceKeys.rdsSecrets();
    expect(key).toEqual(['resources', 'rds', 'secrets']);
  });

  it('all RDS key factories are functions', () => {
    expect(typeof resourceKeys.rdsInstances).toBe('function');
    expect(typeof resourceKeys.rdsClusters).toBe('function');
    expect(typeof resourceKeys.rdsGlueConnections).toBe('function');
    expect(typeof resourceKeys.rdsSecrets).toBe('function');
  });

  it('key tuples are readonly (length 3)', () => {
    expect(resourceKeys.rdsInstances()).toHaveLength(3);
    expect(resourceKeys.rdsClusters()).toHaveLength(3);
    expect(resourceKeys.rdsGlueConnections()).toHaveLength(3);
    expect(resourceKeys.rdsSecrets()).toHaveLength(3);
  });
});
