// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Log retention for Lambda and API Gateway log groups (threat model T-09, R-06).
 *
 * Lambda creates `/aws/lambda/<functionName>` on first invocation with no
 * retention, so logs that can carry customer values (sample rows in error
 * text, prompts, schemas) were kept forever.
 *
 * Mechanism: a standalone `logs.LogRetention` per group, which sets the
 * retention on a group that already exists and creates it if it does not.
 * Rejected:
 *   - `Function.logGroup` with a CDK-generated name: moves every function's
 *     logs away from `/aws/lambda/<functionName>`, which the dashboard's step
 *     log viewer (`handlers/pipelines_logs.py` LAMBDA_LOG_GROUPS) and the
 *     runbook commands in docs/ read by name.
 *   - `Function.logGroup` with the exact `/aws/lambda/<name>` name: CloudFormation
 *     cannot create a group that Lambda already created on a deployed stack.
 *   - `Function.logRetention`: deprecated in aws-cdk-lib.
 *
 * ONE_MONTH matches the Step Functions log group (orchestration-stack.ts).
 * Pinned by test/log-retention.test.ts.
 */
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

export const LOG_RETENTION = logs.RetentionDays.ONE_MONTH;

/**
 * Set LOG_RETENTION on the log group of every `lambda.Function` under `scope`,
 * including the ones CDK creates internally (BucketDeployment, custom
 * resources built on lambda.Function). Call it last in the stack constructor,
 * after every function exists. Returns how many groups it covered.
 */
export function retainLambdaLogs(scope: Construct): number {
  const functions = scope.node
    .findAll()
    .filter((construct): construct is lambda.Function => construct instanceof lambda.Function);
  for (const fn of functions) {
    new logs.LogRetention(fn, 'LogRetention', {
      logGroupName: `/aws/lambda/${fn.functionName}`,
      retention: LOG_RETENTION,
    });
  }
  return functions.length;
}

/** Set LOG_RETENTION on one named log group (e.g. an API Gateway execution log). */
export function retainLogGroup(scope: Construct, id: string, logGroupName: string): logs.LogRetention {
  return new logs.LogRetention(scope, id, { logGroupName, retention: LOG_RETENTION });
}
