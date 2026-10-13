// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import {
  CloudFormationClient,
  DescribeStacksCommand,
} from '@aws-sdk/client-cloudformation';
import { LambdaClient, GetFunctionCommand } from '@aws-sdk/client-lambda';
import {
  DynamoDBClient,
  DescribeTableCommand,
} from '@aws-sdk/client-dynamodb';
import { S3Client, HeadBucketCommand } from '@aws-sdk/client-s3';
import { SFNClient, DescribeStateMachineCommand } from '@aws-sdk/client-sfn';

const region = process.env.AWS_REGION ?? 'us-west-2';
const env = process.env.ENVIRONMENT ?? 'dev';

const cfn = new CloudFormationClient({ region });
const lambdaClient = new LambdaClient({ region });
const ddb = new DynamoDBClient({ region });
const s3 = new S3Client({ region });
const sfnClient = new SFNClient({ region });

const STACK_NAMES = [
  `AgenticDataIngestion-Foundation-${env}`,
  `AgenticDataIngestion-Data-${env}`,
  `AgenticDataIngestion-Compute-${env}`,
  `AgenticDataIngestion-Orchestration-${env}`,
  `AgenticDataIngestion-AgentCore-${env}`,
];

const LAMBDA_FUNCTIONS = [
  'agentic_data_ingestion_file_validation',
  'agentic_data_ingestion_schema_extract',
  'agentic_data_ingestion_schema_compare',
  'agentic_data_ingestion_schema_mapping',
  'agentic_data_ingestion_create_code',
  'agentic_data_ingestion_test_code',
  'agentic_data_ingestion_create_glue_pipeline',
  'agentic_data_ingestion_create_infrastructure',
  'agentic_data_ingestion_create_quality_checks',
  'agentic_data_ingestion_run_pipeline',
  'agentic_data_ingestion_self_healing_dispatch',
  'agentic_data_ingestion_job_completion',
  'agentic_data_ingestion_approval_config',
  'agentic_data_ingestion_dashboard_api',
  'agentic_data_ingestion_approval_handler',
  'agentic_data_ingestion_approval_timeout_handler',
];

const DYNAMODB_TABLES = [
  'agentic_data_ingestion_state',
  'agentic_data_ingestion_pipelines',
  'agentic_data_ingestion_schema_mappings',
  'agentic_data_ingestion_generated_code',
  'agentic_data_ingestion_test_results',
  'agentic_data_ingestion_approvals',
  'agentic_data_ingestion_notifications',
  'agentic_data_ingestion_executions',
  'agentic_data_ingestion_task_tokens',
  'agentic_data_ingestion_ws_connections',
  'agentic_data_ingestion_errors',
  'agentic_data_ingestion_pipeline_locks',
  'agentic_data_ingestion_audit_log',
  'agentic_data_ingestion_agent_traces',
  'agentic_data_ingestion_pipeline_watermarks',
  'agentic_data_ingestion_model_call_budget',
];

const account = process.env.CDK_DEFAULT_ACCOUNT ?? process.env.AWS_ACCOUNT_ID ?? '';

const S3_BUCKETS = [
  `adi-source-files-${account}-${region}`,
  `adi-processed-files-${account}-${region}`,
  `adi-target-schemas-${account}-${region}`,
  `adi-generated-code-${account}-${region}`,
  `adi-test-outputs-${account}-${region}`,
  `adi-templates-${account}-${region}`,
  `adi-lambda-code-${account}-${region}`,
  `adi-dependencies-${account}-${region}`,
];

const STATE_MACHINE_NAME = 'agentic-data-ingestion-pipeline';

// Requires AWS credentials and deployed stacks — run with:
// AWS_ACCOUNT_ID=<your-account-id> npx vitest run test/integration.test.ts
// Live-AWS integration tests: verify deployed stacks/resources exist. Gated
// behind AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true (same convention as the
// Python e2e layer) so `npm test` passes without a deployed environment.
const realAws = process.env.AGENTIC_DATA_INGESTION_TEST_REAL_AWS === 'true';

describe.runIf(realAws)('AgenticDataIngestion CDK Integration Tests', () => {
  describe('CloudFormation Stacks', () => {
    it.each(STACK_NAMES)('stack %s exists and is in a healthy state', async (stackName) => {
      const result = await cfn.send(new DescribeStacksCommand({ StackName: stackName }));
      const stack = result.Stacks?.[0];
      expect(stack).toBeDefined();
      expect(['CREATE_COMPLETE', 'UPDATE_COMPLETE']).toContain(stack!.StackStatus);
    });
  });

  describe('Lambda Functions', () => {
    it.each(LAMBDA_FUNCTIONS)('function %s exists', async (fnName) => {
      const result = await lambdaClient.send(new GetFunctionCommand({ FunctionName: fnName }));
      expect(result.Configuration).toBeDefined();
      expect(result.Configuration!.FunctionName).toBe(fnName);
    });
  });

  describe('DynamoDB Tables', () => {
    it.each(DYNAMODB_TABLES)('table %s exists', async (tableName) => {
      const result = await ddb.send(new DescribeTableCommand({ TableName: tableName }));
      expect(result.Table).toBeDefined();
      expect(result.Table!.TableStatus).toBe('ACTIVE');
    });
  });

  describe('S3 Buckets', () => {
    it.each(S3_BUCKETS)('bucket %s exists', async (bucketName) => {
      // HeadBucket returns 200 if the bucket exists and is accessible
      await expect(
        s3.send(new HeadBucketCommand({ Bucket: bucketName }))
      ).resolves.toBeDefined();
    });
  });

  describe('API Gateway', () => {
    it('API endpoint is configured in the Compute stack outputs', async () => {
      const result = await cfn.send(
        new DescribeStacksCommand({ StackName: `AgenticDataIngestion-Compute-${env}` })
      );
      const stack = result.Stacks?.[0];
      expect(stack).toBeDefined();
      // CDK auto-generates output keys for RestApi URL
      const apiOutput = stack!.Outputs?.find(
        (o) => o.OutputKey?.includes('DashboardApi') || o.OutputValue?.includes('execute-api')
      );
      expect(apiOutput).toBeDefined();
      expect(apiOutput!.OutputValue).toMatch(/https:\/\/.*\.execute-api\..*\.amazonaws\.com/);
    });
  });

  describe('Step Functions', () => {
    it('state machine exists', async () => {
      const accountId = process.env.CDK_DEFAULT_ACCOUNT ?? process.env.AWS_ACCOUNT_ID;
      const arn = `arn:aws:states:${region}:${accountId}:stateMachine:${STATE_MACHINE_NAME}`;
      const result = await sfnClient.send(
        new DescribeStateMachineCommand({ stateMachineArn: arn })
      );
      expect(result.name).toBe(STATE_MACHINE_NAME);
      expect(result.status).toBe('ACTIVE');
    });
  });
});
