// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The JDBC credential requirement, on the step that owns the field.
 *
 * A JDBC target in a mode where AgenticDataIngestion CREATES the Glue
 * Connection needs a `secret_arn`. `create_glue_jdbc_connection` sets
 * `SECRET_ID` only when one is present; without it the connection carries
 * `JDBC_CONNECTION_URL` alone, and Glue rejects that with the generic
 * "Validation for connection properties failed" (`LGR-819980f-2`).
 *
 * `create_pipeline` enforces this at the API boundary. These tests pin the
 * client side of it, which exists for one reason: before it, the wizard let
 * the user reach the final Create step and answer with a wall-of-text 400
 * about a field four steps back. Two halves:
 *
 *   1. The secret control says it is required, inline, while the user is
 *      looking at it.
 *   2. Forward navigation from the Target step is blocked until it is
 *      satisfied — the same shape as the `write_mode` and write-table guards
 *      already in `CreatePipelinePage`.
 *
 * The exemption is keyed on `glue_connection_name`, NOT the mode string,
 * because `_provision_jdbc_target_connection` early-returns on that field in
 * ANY mode: no connection is created, so the user's existing one supplies the
 * credentials. Keying on the field is what makes the client check agree with
 * the backend one instead of approximating it.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { JdbcConnectionSelector } from '../JdbcConnectionSelector';

const SECRET_ARN = 'arn:aws:secretsmanager:us-west-2:123456789012:secret:adi/mydb-AbCdEf';

vi.mock('../../../hooks', async () => {
  const actual = await vi.importActual('../../../hooks');
  return {
    ...actual,
    useRdsInstances: () => ({ data: [], isLoading: false }),
    useRdsClusters: () => ({ data: [], isLoading: false }),
    useRdsGlueConnections: () => ({ data: [], isLoading: false }),
    useRdsSecrets: () => ({
      data: [{ name: 'adi/mydb', arn: SECRET_ARN, description: '', last_changed_date: '' }],
      isLoading: false,
    }),
  };
});

const REQUIRED_MESSAGE = 'A credentials secret is required';

function renderSelector(config: Record<string, string>) {
  return render(<JdbcConnectionSelector config={config} onChange={vi.fn()} />);
}

/* ---------- 1. the control states the requirement inline ---------- */

describe('select_rds secret field — inline requirement', () => {
  it('shows the requirement when no secret is chosen', () => {
    renderSelector({ _connection_mode: 'select_rds' });
    expect(screen.getByText(REQUIRED_MESSAGE)).toBeInTheDocument();
  });

  it('clears the requirement once a secret is chosen', () => {
    renderSelector({ _connection_mode: 'select_rds', secret_arn: SECRET_ARN });
    expect(screen.queryByText(REQUIRED_MESSAGE)).not.toBeInTheDocument();
  });

  it('says nothing in glue_connection mode, which renders no secret control', () => {
    renderSelector({ _connection_mode: 'glue_connection' });
    expect(screen.queryByText(REQUIRED_MESSAGE)).not.toBeInTheDocument();
  });

  it('defaults to select_rds when no mode is set, so the requirement shows', () => {
    // Matches the backend default: a payload with no `_connection_mode` is
    // `select_rds` (Req 12.1). This is the exact shape that reached HTTP 201
    // without a credential and then failed at step 8.
    renderSelector({});
    expect(screen.getByText(REQUIRED_MESSAGE)).toBeInTheDocument();
  });
});

/* ---------- 2. the Target-step navigation guard ---------- */

/**
 * Mirror of `CreatePipelinePage`'s `isJdbcTargetSecretMissing`. The predicate
 * lives in the page rather than a module, so the sibling guard tests
 * (`JdbcTargetForm.write-mode.test.tsx`, `WizardStepSchema.mirror-source.test.tsx`)
 * re-implement theirs the same way. Keep this in step with the original.
 */
function secretMissing(
  targetType: string,
  targetConfig: Record<string, string | undefined>,
): boolean {
  return (
    targetType === 'jdbc' &&
    (targetConfig._connection_mode ?? 'select_rds') !== 'glue_connection' &&
    !targetConfig.glue_connection_name &&
    !targetConfig.secret_arn
  );
}

describe('Target step Next guard — isJdbcTargetSecretMissing', () => {
  it('blocks a select_rds target with no secret', () => {
    expect(secretMissing('jdbc', { _connection_mode: 'select_rds' })).toBe(true);
  });

  it('blocks a legacy target carrying no _connection_mode at all', () => {
    expect(secretMissing('jdbc', { jdbc_url: 'jdbc:postgresql://h:5432/d' })).toBe(true);
  });

  it('blocks a manual target with no secret — that mode creates the connection too', () => {
    expect(secretMissing('jdbc', { _connection_mode: 'manual' })).toBe(true);
  });

  it('allows a select_rds target once the secret is set', () => {
    expect(secretMissing('jdbc', { _connection_mode: 'select_rds', secret_arn: SECRET_ARN })).toBe(
      false,
    );
  });

  it('allows glue_connection mode, which supplies its own credentials', () => {
    expect(secretMissing('jdbc', { _connection_mode: 'glue_connection' })).toBe(false);
  });

  it('allows a named existing connection in ANY mode', () => {
    // The exemption keys on the field, not the mode — matching
    // `_provision_jdbc_target_connection`'s early return.
    expect(
      secretMissing('jdbc', {
        _connection_mode: 'select_rds',
        glue_connection_name: 'customer-owned-postgres',
      }),
    ).toBe(false);
  });

  it('ignores non-JDBC targets entirely', () => {
    for (const targetType of ['s3_tables', 'glue_catalog', 's3_parquet']) {
      expect(secretMissing(targetType, {})).toBe(false);
    }
  });
});
