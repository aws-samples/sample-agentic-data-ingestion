// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';

/**
 * CDK assertion tests for the Approval_Config_Role CloudWatch grant
 * (P28b R2, task 3.1) plus the R3 dynamodb:DeleteItem addition.
 *
 * The notification service's CloudWatch channel
 * (shared/notification_service.py CloudWatchChannel) runs under the
 * approval-config role and issues, for every critical alert:
 *
 *   1. cloudwatch:PutMetricData into namespace AgenticDataIngestion/Alerts
 *      (the action defines no resource types — it is bounded by the
 *      cloudwatch:namespace condition instead),
 *   2. cloudwatch:PutMetricAlarm against alarms named
 *      AgenticDataIngestion-Critical-* (the action supports the alarm
 *      resource type, so the statement is resource-scoped to that ARN
 *      pattern),
 *   3. cloudwatch:DescribeAlarms via the documented R2.3 fallback: the
 *      channel describes by AlarmNamePrefix and IAM authorizes such requests
 *      against alarm:* — a prefix-scoped resource denies in practice
 *      (verified live during the p28b task 6.1 check: AccessDenied on
 *      DescribeAlarms under the AgenticDataIngestion-Critical-* scope), so
 *      this single read-only action gets its own Resource-'*' statement.
 *
 * Without the grant every critical notification dies with AccessDenied
 * (flagged in p28 task 15.3). The per-approval-id create_timeout_alarm path
 * (namespace AgenticDataIngestion/Approvals, alarm prefix
 * AgenticDataIngestion-Approval-Timeout-) intentionally stays OUTSIDE the
 * grant — recorded p28b R6.1 decision; a test here must never assert it in.
 *
 * The same role also gains dynamodb:DeleteItem (P28b R3): the approval and
 * timeout handlers delete the task-token record AFTER a confirmed SendTask*
 * callback (send-before-status ordering).
 *
 * Validates: Requirements 2.1, 2.2, 2.3, 2.5
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';

const ALERTS_NAMESPACE = 'AgenticDataIngestion/Alerts';
const CRITICAL_ALARM_ARN_PATTERN = `arn:aws:cloudwatch:${TEST_REGION}:${TEST_ACCOUNT}:alarm:AgenticDataIngestion-Critical-*`;
const ALARM_ACTIONS = ['cloudwatch:PutMetricAlarm', 'cloudwatch:DescribeAlarms'];
const SCOPED_ALARM_ACTIONS = ['cloudwatch:PutMetricAlarm'];

describe('FoundationStack — Approval_Config_Role CloudWatch grant (P28b R2)', () => {
  let json: Record<string, any>;

  beforeAll(() => {
    const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
    const stack = new FoundationStack(app, `AgenticDataIngestion-Foundation-${TEST_ENV}`, {
      env: { account: TEST_ACCOUNT, region: TEST_REGION },
      projectName: 'agentic-data-ingestion',
      environment: TEST_ENV,
    });
    json = Template.fromStack(stack).toJSON();
  });

  /** Every policy statement attached (inline or via AWS::IAM::Policy) to the approval-config role. */
  function approvalConfigRoleStatements(): any[] {
    const resources = json.Resources ?? {};
    const roleKey = Object.keys(resources).find(
      (k) =>
        resources[k].Type === 'AWS::IAM::Role' &&
        resources[k].Properties?.RoleName ===
          `agentic-data-ingestion-${TEST_ENV}-approvalconfig-role`,
    );
    expect(roleKey, 'approval-config role must exist').toBeDefined();

    const statements: any[] = [];
    // Inline policies on the role itself
    for (const inline of resources[roleKey!].Properties?.Policies ?? []) {
      statements.push(...(inline.PolicyDocument?.Statement ?? []));
    }
    // addToPolicy() statements land in AWS::IAM::Policy resources whose
    // Roles list references the role's logical id.
    for (const resource of Object.values(resources) as any[]) {
      if (resource.Type !== 'AWS::IAM::Policy') continue;
      const attached = (resource.Properties?.Roles ?? []).some(
        (r: any) => r?.Ref === roleKey,
      );
      if (attached) statements.push(...(resource.Properties?.PolicyDocument?.Statement ?? []));
    }
    return statements;
  }

  function asArray(value: any): any[] {
    return Array.isArray(value) ? value : [value];
  }

  it('grants all three CloudWatch actions to the approval-config role', () => {
    const allActions = approvalConfigRoleStatements().flatMap((s: any) => asArray(s.Action));
    for (const action of ['cloudwatch:PutMetricData', ...ALARM_ACTIONS]) {
      expect(allActions, `${action} must be granted to the approval-config role`).toContain(action);
    }
  });

  it('the PutMetricData statement is bounded by the cloudwatch:namespace condition (AgenticDataIngestion/Alerts)', () => {
    const statements = approvalConfigRoleStatements();
    const putMetricDataStmt = statements.find((s: any) =>
      asArray(s.Action).includes('cloudwatch:PutMetricData'),
    );
    expect(
      putMetricDataStmt,
      'the channel must be able to publish critical-alert metrics',
    ).toBeDefined();
    expect(putMetricDataStmt.Effect).toBe('Allow');
    // PutMetricData defines no resource types — Resource '*' bounded by the
    // namespace condition is the only supported scoping (R2.2).
    expect(asArray(putMetricDataStmt.Resource)).toEqual(['*']);
    expect(putMetricDataStmt.Condition).toEqual({
      StringEquals: { 'cloudwatch:namespace': ALERTS_NAMESPACE },
    });
    // The wildcard resource stays narrow: no alarm actions ride along on the
    // condition-bounded statement (they carry their own resource scope).
    for (const alarmAction of ALARM_ACTIONS) {
      expect(asArray(putMetricDataStmt.Action)).not.toContain(alarmAction);
    }
  });

  it('PutMetricAlarm is resource-scoped to the AgenticDataIngestion-Critical-* alarm-ARN pattern', () => {
    const statements = approvalConfigRoleStatements();
    for (const action of SCOPED_ALARM_ACTIONS) {
      const stmt = statements.find((s: any) => asArray(s.Action).includes(action));
      expect(stmt, `${action} must be granted`).toBeDefined();
      expect(stmt.Effect).toBe('Allow');
      expect(
        asArray(stmt.Resource),
        `${action} must be scoped to the critical-alert alarm prefix, never '*'`,
      ).toEqual([CRITICAL_ALARM_ARN_PATTERN]);
    }
  });

  it('DescribeAlarms rides its own read-only Resource-* statement (documented R2.3 fallback, verified live)', () => {
    // The channel describes by AlarmNamePrefix; IAM authorizes DescribeAlarms
    // against alarm:*, so the AgenticDataIngestion-Critical-* resource scope
    // denied in practice (p28b task 6.1 live check: AccessDenied). R2.3's
    // fallback clause: that single action gets a separate statement with
    // Resource '*'. The statement must carry ONLY the read-only describe —
    // no write action may ride along on the wildcard.
    const statements = approvalConfigRoleStatements();
    const stmt = statements.find((s: any) =>
      asArray(s.Action).includes('cloudwatch:DescribeAlarms'),
    );
    expect(stmt, 'cloudwatch:DescribeAlarms must be granted').toBeDefined();
    expect(stmt.Effect).toBe('Allow');
    expect(asArray(stmt.Resource)).toEqual(['*']);
    expect(
      asArray(stmt.Action),
      'the Resource-* fallback statement must carry only the read-only DescribeAlarms',
    ).toEqual(['cloudwatch:DescribeAlarms']);
  });

  it('does NOT grant the per-approval-id timeout-alarm path (recorded R6.1 decision)', () => {
    // The create_timeout_alarm path (namespace AgenticDataIngestion/Approvals,
    // alarm prefix AgenticDataIngestion-Approval-Timeout-) stays outside the
    // grant. Nothing on the role may name that namespace or alarm prefix.
    const flattened = JSON.stringify(approvalConfigRoleStatements());
    expect(flattened).not.toContain('AgenticDataIngestion/Approvals');
    expect(flattened).not.toContain('AgenticDataIngestion-Approval-Timeout-');
  });

  it('grants dynamodb:DeleteItem on the role DynamoDB statement (P28b R3 token-record delete)', () => {
    const statements = approvalConfigRoleStatements();
    const dynamoStmt = statements.find((s: any) =>
      asArray(s.Action).includes('dynamodb:GetItem'),
    );
    expect(dynamoStmt, 'the role DynamoDB statement must exist').toBeDefined();
    expect(dynamoStmt.Effect).toBe('Allow');
    expect(
      asArray(dynamoStmt.Action),
      'the post-send token-record delete needs dynamodb:DeleteItem',
    ).toContain('dynamodb:DeleteItem');
    // The delete rides the existing table-scoped statement, not a wildcard.
    for (const resource of asArray(dynamoStmt.Resource)) {
      expect(resource).toContain(':table/agentic_data_ingestion');
    }
  });
});
