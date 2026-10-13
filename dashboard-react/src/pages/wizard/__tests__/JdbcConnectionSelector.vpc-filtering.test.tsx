// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Example tests for VPC-scoped Subnet / Security Group dropdowns in
 * <ManualConnectionForm>.
 *
 * Covers P20 Task 8.12:
 *   - Subnet and SG dropdowns disabled when vpc_id unset (Req 3.3, 3.4)
 *   - Error from useVpcs() shows inline <Alert type="error"> (Req 3.9)
 *   - testIdPrefix disambiguates source-side and target-side selectors
 *     when both render on the same page (Req 1.7)
 *
 * Validates: P20 Requirements 1.7, 3.3, 3.4, 3.9, 14.4
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';

const useVpcsMock = vi.fn();
const useSubnetsMock = vi.fn();
const useSecurityGroupsMock = vi.fn();

vi.mock('../../../hooks', async () => {
  const actual = await vi.importActual('../../../hooks');
  return {
    ...actual,
    useRdsInstances: () => ({ data: [], isLoading: false }),
    useRdsClusters: () => ({ data: [], isLoading: false }),
    useRdsGlueConnections: () => ({ data: [], isLoading: false }),
    useRdsSecrets: () => ({ data: [], isLoading: false }),
    useVpcs: () => useVpcsMock(),
    useSubnets: (vpcId: string | undefined | null) => useSubnetsMock(vpcId),
    useSecurityGroups: (vpcId: string | undefined | null) =>
      useSecurityGroupsMock(vpcId),
    useJdbcCandidateSecrets: () => ({
      data: [],
      isLoading: false,
      error: null,
    }),
  };
});

import { JdbcConnectionSelector } from '../JdbcConnectionSelector';

function renderSelector(
  initialConfig: Record<string, string> = {},
  testIdPrefix = 'jdbc-conn',
) {
  const onChange = vi.fn();
  render(
    <JdbcConnectionSelector
      config={initialConfig}
      onChange={onChange}
      testIdPrefix={testIdPrefix}
    />,
  );
  return { onChange };
}

describe('JdbcConnectionSelector — VPC filtering (P20)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
    // Default mocks — non-error, non-loading.
    useVpcsMock.mockReturnValue({
      data: [
        {
          vpc_id: 'vpc-aaaa1111',
          name: 'AgenticDataIngestion-dev',
          is_default: false,
          cidr: '10.0.0.0/16',
        },
      ],
      isLoading: false,
      error: null,
    });
    useSubnetsMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: null,
    });
    useSecurityGroupsMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: null,
    });
  });

  // -------------------------------------------------------------
  // Req 3.3 — Subnet <Select> disabled when vpc_id unset
  // -------------------------------------------------------------

  it('subnet <Select> renders an aria-disabled trigger when vpc_id is unset', () => {
    // Bypass the pre-selection effect by returning empty vpcs.
    useVpcsMock.mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
    });
    renderSelector({ _connection_mode: 'manual' });
    const subnetSelect = screen.getByTestId('jdbc-conn-manual-subnet');
    // Cloudscape Select reflects the disabled state via aria-disabled
    // on the trigger placeholder span.
    const trigger = subnetSelect.querySelector('[aria-disabled="true"]');
    expect(trigger).not.toBeNull();
  });

  // -------------------------------------------------------------
  // Req 3.4 — SG <Multiselect> disabled when vpc_id unset
  // -------------------------------------------------------------

  it('security-group multiselect renders an aria-disabled trigger when vpc_id is unset', () => {
    useVpcsMock.mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
    });
    renderSelector({ _connection_mode: 'manual' });
    const sgEl = screen.getByTestId('jdbc-conn-manual-sg');
    const trigger = sgEl.querySelector('[aria-disabled="true"]');
    expect(trigger).not.toBeNull();
  });

  // -------------------------------------------------------------
  // Req 3.9 — Error renders inline <Alert>
  // -------------------------------------------------------------

  it('shows an error alert when useVpcs() returns an error', () => {
    useVpcsMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('boom'),
    });
    renderSelector({ _connection_mode: 'manual' });
    expect(screen.getByTestId('jdbc-conn-manual-vpc-error')).toBeInTheDocument();
    expect(screen.getByText(/Failed to load VPCs/)).toBeInTheDocument();
  });

  it('shows an error alert when useSubnets() returns an error', () => {
    useSubnetsMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('subnet boom'),
    });
    renderSelector({
      _connection_mode: 'manual',
      vpc_id: 'vpc-aaaa1111',
    });
    expect(
      screen.getByTestId('jdbc-conn-manual-subnet-error'),
    ).toBeInTheDocument();
    expect(screen.getByText(/Failed to load subnets/)).toBeInTheDocument();
  });

  it('shows an error alert when useSecurityGroups() returns an error', () => {
    useSecurityGroupsMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('sg boom'),
    });
    renderSelector({
      _connection_mode: 'manual',
      vpc_id: 'vpc-aaaa1111',
    });
    expect(
      screen.getByTestId('jdbc-conn-manual-sg-error'),
    ).toBeInTheDocument();
    expect(screen.getByText(/Failed to load security groups/)).toBeInTheDocument();
  });

  // -------------------------------------------------------------
  // Req 1.7 — testIdPrefix disambiguates
  // -------------------------------------------------------------

  it('testIdPrefix prefixes ALL manual-mode test-ids', () => {
    renderSelector(
      { _connection_mode: 'manual' },
      'custom-prefix',
    );
    expect(
      screen.getByTestId('custom-prefix-manual-engine'),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId('custom-prefix-manual-jdbc-url'),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId('custom-prefix-manual-vpc'),
    ).toBeInTheDocument();
  });

  // -------------------------------------------------------------
  // P29 R1.3 — inline error carries a Retry that re-triggers the query
  // -------------------------------------------------------------

  it('renders a Retry button on the VPC error alert that re-triggers the query', () => {
    const refetchVpcs = vi.fn();
    useVpcsMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('boom'),
      refetch: refetchVpcs,
    });
    renderSelector({ _connection_mode: 'manual' });

    // Inline error is shown (not an empty/silent dropdown).
    expect(screen.getByTestId('jdbc-conn-manual-vpc-error')).toBeInTheDocument();

    // Clicking Retry re-triggers the fetch.
    const retry = screen.getByTestId('jdbc-conn-manual-vpc-retry');
    fireEvent.click(retry);
    expect(refetchVpcs).toHaveBeenCalledTimes(1);
  });
});
