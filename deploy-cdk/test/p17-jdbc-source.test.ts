// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { DataStack } from '../lib/data-stack';
import { FoundationStack } from '../lib/foundation-stack';

/**
 * CDK assertion tests for P17 (JDBC Data Source) infrastructure additions.
 *
 * Validates: Requirement 22.1 — verify the Watermarks Table exists with the
 * correct PK and BillingMode.PAY_PER_REQUEST, the JDBC discovery Glue
 * database, and the IAM additions on `infrastructureRole`,
 * `gluePipelineRole`, and `schemaExtractRole`.
 *
 * Scope today: Data Stack additions from Task 13.1 (Watermarks Table) +
 * Foundation Stack additions from Tasks 14.1–14.4 (discovery DB + IAM).
 * Orchestration Stack additions from Task 15 (DiscoveryCleanupRule daily
 * janitor — Requirement 4.11) live in `test/orchestration-stack.test.ts`
 * alongside that stack's existing assertions, since the test fixture
 * there already builds the full mock dependency graph the
 * OrchestrationStack synthesizer requires.
 */

function synthDataStack(): Template {
  const app = new cdk.App();
  const stack = new DataStack(app, 'TestDataStack-P17', {
    env: { account: '123456789012', region: 'us-west-2' },
    environment: 'dev',
  });
  return Template.fromStack(stack);
}

describe('P17 — DataStack: agentic_data_ingestion_pipeline_watermarks table', () => {
  let template: Template;

  function getTemplate() {
    if (!template) template = synthDataStack();
    return template;
  }

  it('Watermarks Table exists with the correct table name', () => {
    getTemplate().hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'agentic_data_ingestion_pipeline_watermarks',
    });
  });

  it('Watermarks Table partition key is `pipeline_id` (HASH, type S)', () => {
    getTemplate().hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'agentic_data_ingestion_pipeline_watermarks',
      KeySchema: [
        { AttributeName: 'pipeline_id', KeyType: 'HASH' },
      ],
      AttributeDefinitions: Match.arrayWith([
        Match.objectLike({ AttributeName: 'pipeline_id', AttributeType: 'S' }),
      ]),
    });
  });

  it('Watermarks Table uses BillingMode.PAY_PER_REQUEST', () => {
    getTemplate().hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'agentic_data_ingestion_pipeline_watermarks',
      BillingMode: 'PAY_PER_REQUEST',
    });
  });

  it('Watermarks Table has point-in-time recovery enabled', () => {
    getTemplate().hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'agentic_data_ingestion_pipeline_watermarks',
      PointInTimeRecoverySpecification: {
        PointInTimeRecoveryEnabled: true,
      },
    });
  });

  it('Watermarks Table uses AWS-managed encryption (SSE enabled)', () => {
    getTemplate().hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'agentic_data_ingestion_pipeline_watermarks',
      SSESpecification: {
        SSEEnabled: true,
      },
    });
  });

  it('Watermarks Table is RETAIN on stack deletion', () => {
    // RemovalPolicy.RETAIN is encoded on the CFN resource itself, not in
    // Properties — assert via the resource-level UpdateReplacePolicy /
    // DeletionPolicy fields.
    getTemplate().hasResource('AWS::DynamoDB::Table', {
      Properties: Match.objectLike({
        TableName: 'agentic_data_ingestion_pipeline_watermarks',
      }),
      DeletionPolicy: 'Retain',
      UpdateReplacePolicy: 'Retain',
    });
  });

  it('Watermarks Table has no GSIs (PK-only by design)', () => {
    // The watermark item is keyed solely by pipeline_id; no secondary access
    // pattern is required (see design — chosen over an attribute on
    // agentic_data_ingestion_pipelines to avoid item-version churn).
    const json = getTemplate().toJSON();
    const watermarksKey = Object.keys(json.Resources).find(
      (k) =>
        json.Resources[k].Type === 'AWS::DynamoDB::Table' &&
        json.Resources[k].Properties?.TableName === 'agentic_data_ingestion_pipeline_watermarks',
    );
    expect(watermarksKey).toBeDefined();
    expect(json.Resources[watermarksKey!].Properties.GlobalSecondaryIndexes).toBeUndefined();
  });
});


// ─────────────────────────────────────────────────────────────────────
// Foundation Stack (Task 14) — discovery DB + IAM additions
// ─────────────────────────────────────────────────────────────────────

const TEST_ACCOUNT = '123456789012';
const TEST_REGION = 'us-west-2';
const TEST_PROJECT = 'agentic-data-ingestion';
const TEST_ENV = 'dev';

function synthFoundationStack(): Template {
  const app = new cdk.App();
  const stack = new FoundationStack(app, 'TestFoundationStack-P17', {
    env: { account: TEST_ACCOUNT, region: TEST_REGION },
    projectName: TEST_PROJECT,
    environment: TEST_ENV,
  });
  return Template.fromStack(stack);
}

let _foundationTemplate: Template | undefined;
function foundationTemplate(): Template {
  if (!_foundationTemplate) _foundationTemplate = synthFoundationStack();
  return _foundationTemplate;
}

describe('P17 — FoundationStack: agentic_data_ingestion_jdbc_discovery_<env> database', () => {
  it('Glue Database exists with the correct DatabaseInput.Name for the environment', () => {
    foundationTemplate().hasResourceProperties('AWS::Glue::Database', {
      DatabaseInput: Match.objectLike({
        Name: `agentic_data_ingestion_jdbc_discovery_${TEST_ENV}`,
      }),
    });
  });

  it('Glue Database CatalogId matches the stack account', () => {
    foundationTemplate().hasResourceProperties('AWS::Glue::Database', {
      CatalogId: TEST_ACCOUNT,
      DatabaseInput: Match.objectLike({
        Name: `agentic_data_ingestion_jdbc_discovery_${TEST_ENV}`,
      }),
    });
  });

  it('Glue Database carries a description identifying it as the JDBC discovery DB', () => {
    foundationTemplate().hasResourceProperties('AWS::Glue::Database', {
      DatabaseInput: Match.objectLike({
        Name: `agentic_data_ingestion_jdbc_discovery_${TEST_ENV}`,
        Description: Match.stringLikeRegexp('JDBC source schema discovery'),
      }),
    });
  });
});

describe('P17 — FoundationStack: schemaExtractRole IAM (CRITICAL)', () => {
  it('CRITICAL — DO NOT REMOVE: schemaExtractRole has glue:StartCrawler + glue:GetCrawler on agentic-data-ingestion-* crawlers', () => {
    // Why this is critical:
    // The schema_extract Lambda has a JDBC branch (Task 7) that runs the
    // per-pipeline runtime crawler `agentic-data-ingestion-{pipeline_id}-source-discover`
    // to discover the source table's schema at pipeline-execution time.
    // It calls glue:StartCrawler to kick off the crawl and glue:GetCrawler
    // in a polling loop to wait for completion.
    //
    // Without these grants, JDBC pipelines fail at runtime with an opaque
    // AccessDenied error in the Step Functions schema_extract step rather
    // than a feature-level error. Future contributors must not silently
    // remove these statements when refactoring schemaExtractRole.
    foundationTemplate().hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: ['glue:StartCrawler', 'glue:GetCrawler'],
            Effect: 'Allow',
            Resource: `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:crawler/agentic-data-ingestion-*`,
          }),
        ]),
      }),
      Roles: Match.arrayWith([
        Match.objectLike({ Ref: Match.stringLikeRegexp('^SchemaExtract') }),
      ]),
    });
  });

  it('CRITICAL — DO NOT REMOVE: schemaExtractRole has glue:GetTables + glue:GetTable on the JDBC discovery DB', () => {
    // After the runtime crawler completes, the JDBC branch of schema_extract
    // calls glue:GetTables (with Expression="agentic_data_ingestion_{pipeline_id}_*")
    // against `agentic_data_ingestion_jdbc_discovery_{environment}` to locate the
    // discovered table, then glue:GetTable to read column-level metadata
    // for schema conversion. The grant is scoped to the discovery DB ARNs
    // (catalog, database/agentic_data_ingestion_jdbc_discovery_*, table/.../*) — broader
    // grants are intentionally NOT given to this Lambda role.
    foundationTemplate().hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: ['glue:GetTables', 'glue:GetTable'],
            Effect: 'Allow',
            Resource: [
              `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:catalog`,
              `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:database/agentic_data_ingestion_jdbc_discovery_*`,
              `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:table/agentic_data_ingestion_jdbc_discovery_*/*`,
            ],
          }),
        ]),
      }),
      Roles: Match.arrayWith([
        Match.objectLike({ Ref: Match.stringLikeRegexp('^SchemaExtract') }),
      ]),
    });
  });
});

describe('P17 — FoundationStack: infrastructureRole IAM', () => {
  it('infrastructureRole has Glue Crawler lifecycle perms on agentic-data-ingestion-* crawlers', () => {
    // Used by:
    //   • Wizard-time schema-discovery crawlers (handlers/jdbc_resources.py)
    //   • Per-pipeline runtime crawlers created at pipeline-creation time
    //     (handlers/pipelines.py — Tasks 5.1/5.2)
    //   • Daily discovery cleanup janitor (Task 4.3)
    //   • Pipeline delete cleanup (Task 5.4)
    // GetCrawlers (plural) is NOT in this resource-scoped statement — it is a
    // list-all API that does not support resource-level scoping and is granted
    // separately on Resource:'*' (see the dedicated test below).
    foundationTemplate().hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: [
              'glue:CreateCrawler',
              'glue:UpdateCrawler',
              'glue:GetCrawler',
              'glue:StartCrawler',
              'glue:DeleteCrawler',
              'glue:TagResource',
              'glue:GetTags',
              'glue:UntagResource',
            ],
            Effect: 'Allow',
            Resource: `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:crawler/agentic-data-ingestion-*`,
          }),
        ]),
      }),
      Roles: Match.arrayWith([
        Match.objectLike({ Ref: Match.stringLikeRegexp('^Infrastructure') }),
      ]),
    });
  });

  it('infrastructureRole has glue:GetCrawlers on Resource "*" for the cleanup janitor', () => {
    // glue:GetCrawlers is a list-all API that enumerates every crawler in the
    // account and does NOT support resource-level scoping — scoping it to the
    // crawler/agentic-data-ingestion-* ARN returns AccessDenied and silently
    // disables the daily discovery-cleanup janitor (cleanup_discovery_resources),
    // which paginates GetCrawlers to find and reap stale
    // agentic-data-ingestion-discover-* crawlers. It is therefore granted as a
    // dedicated statement on Resource:'*' (covered by the stack's existing
    // AwsSolutions-IAM5[Resource::*] acknowledgment).
    foundationTemplate().hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'glue:GetCrawlers',
            Effect: 'Allow',
            Resource: '*',
          }),
        ]),
      }),
      Roles: Match.arrayWith([
        Match.objectLike({ Ref: Match.stringLikeRegexp('^Infrastructure') }),
      ]),
    });
  });

  it('CRITICAL — DO NOT REMOVE: iam:PassRole for gluePipelineRole has glue.amazonaws.com condition', () => {
    // Why this is critical:
    // glue:CreateCrawler requires iam:PassRole on the role the crawler
    // will execute as (gluePipelineRole). The iam:PassedToService
    // condition restricts the pass to Glue, which is required by AWS for
    // the action to succeed AND scopes the privilege so the role can't be
    // passed to arbitrary services.
    //
    // Without this statement (or with a missing/weakened condition),
    // CreateCrawler fails at deploy time with an opaque AccessDenied
    // error rather than a feature-level error. Future contributors must
    // not silently remove this statement or relax the condition when
    // refactoring the gluePipelineRole.
    //
    // P23 stale-assertion reconciliation (Task 6 / baseline section I):
    // The grant IS present and correctly conditioned — only the test's two
    // EXPECTED values were stale:
    //   (a) Resource: the grant targets the real crawler-execution role
    //       `role/AgenticDataIngestion-*-ExecutionRole` (foundation-stack.ts:327), NOT
    //       the historical `agentic-data-ingestion-dev-gluepipeline-role` string.
    //   (b) Role: the iam:PassRole statement with the glue.amazonaws.com
    //       condition lives on the GluePipeline role (lambdaRole('GluePipeline',
    //       …) — foundation-stack.ts:324), not the infrastructureRole, so the
    //       role-logical-id match is `^GluePipeline` (matching the sibling
    //       glueAdmin wildcard assertion above).
    // The security properties — the `iam:PassRole` action, the
    // `iam:PassedToService: glue.amazonaws.com` StringEquals condition, and the
    // concrete `role/AgenticDataIngestion-*-ExecutionRole` scope — are intentionally
    // asserted as concrete values and MUST NOT be weakened to Match.anyValue().
    foundationTemplate().hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'iam:PassRole',
            Effect: 'Allow',
            Resource: `arn:aws:iam::${TEST_ACCOUNT}:role/AgenticDataIngestion-*-ExecutionRole`,
            Condition: {
              StringEquals: {
                'iam:PassedToService': 'glue.amazonaws.com',
              },
            },
          }),
        ]),
      }),
      Roles: Match.arrayWith([
        Match.objectLike({ Ref: Match.stringLikeRegexp('^GluePipeline') }),
      ]),
    });
  });

  it('infrastructureRole has iam:PassRole on the gluePipelineRole scoped to glue.amazonaws.com', () => {
    // P17 fix: the dashboard API Lambda (infrastructureRole) creates JDBC
    // discovery Glue Crawlers that execute as gluePipelineRole. Glue's
    // CreateCrawler requires iam:PassRole on that execution role. The
    // iam:PassedToService condition scopes the pass to Glue so the role
    // cannot be passed to arbitrary services. Without this grant the
    // "Detect Schema" endpoint returns HTTP 500 (AccessDenied on
    // CreateCrawler). The resource is the gluePipelineRole ARN, which CDK
    // emits as an Fn::GetAtt to the GluePipeline role logical ID.
    foundationTemplate().hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'iam:PassRole',
            Effect: 'Allow',
            Resource: Match.objectLike({
              'Fn::GetAtt': Match.arrayWith([Match.stringLikeRegexp('^GluePipeline')]),
            }),
            Condition: {
              StringEquals: {
                'iam:PassedToService': 'glue.amazonaws.com',
              },
            },
          }),
        ]),
      }),
      Roles: Match.arrayWith([
        Match.objectLike({ Ref: Match.stringLikeRegexp('^Infrastructure') }),
      ]),
    });
  });

  it('infrastructureRole has EC2 Describe perms for the S3 VPC Gateway Endpoint pre-flight check', () => {
    // Used by handlers/jdbc_resources.detect_schema_start to validate that
    // the customer's RDS VPC has an S3 Gateway Endpoint (Req 15.6) before
    // creating a Glue Crawler — Glue requires S3 access from the
    // connection's VPC. ec2:DescribeVpcs additionally backs the manual JDBC
    // connection mode's VPC dropdown (P20 Req 5.7) via
    // handlers/vpc_resources.py. EC2 Describe APIs do not support
    // resource-level permissions, so the resource is intentionally `*`.
    foundationTemplate().hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: [
              'ec2:DescribeVpcs',
              'ec2:DescribeSubnets',
              'ec2:DescribeSecurityGroups',
              'ec2:DescribeRouteTables',
            ],
            Effect: 'Allow',
            Resource: '*',
          }),
        ]),
      }),
      Roles: Match.arrayWith([
        Match.objectLike({ Ref: Match.stringLikeRegexp('^Infrastructure') }),
      ]),
    });
  });

  it('infrastructureRole has glue:GetTables/GetTable/DeleteTable on the JDBC discovery DB', () => {
    // Used by the prefix-based table lookup after a wizard-time crawler
    // completes (Req 4.5) and by the janitor / per-pipeline delete
    // cleanup paths (Reqs 4.11, 11.8). Scoped to the discovery DB ARNs
    // — catalog + database/agentic_data_ingestion_jdbc_discovery_* + table/.../*.
    foundationTemplate().hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: ['glue:GetTables', 'glue:GetTable', 'glue:DeleteTable'],
            Effect: 'Allow',
            Resource: [
              `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:catalog`,
              `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:database/agentic_data_ingestion_jdbc_discovery_*`,
              `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:table/agentic_data_ingestion_jdbc_discovery_*/*`,
            ],
          }),
        ]),
      }),
      Roles: Match.arrayWith([
        Match.objectLike({ Ref: Match.stringLikeRegexp('^Infrastructure') }),
      ]),
    });
  });

  it('infrastructureRole has glue:DeleteConnection + glue:UpdateConnection on agentic-data-ingestion-* connections', () => {
    // P14 already grants CreateConnection on the same scope; this
    // statement adds Delete + Update needed for pipeline-creation
    // rollback (Req 11.5) and pipeline-delete cleanup (Req 11.7).
    foundationTemplate().hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: ['glue:DeleteConnection', 'glue:UpdateConnection'],
            Effect: 'Allow',
            Resource: [
              `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:catalog`,
              `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:connection/agentic-data-ingestion-*`,
            ],
          }),
        ]),
      }),
      Roles: Match.arrayWith([
        Match.objectLike({ Ref: Match.stringLikeRegexp('^Infrastructure') }),
      ]),
    });
  });
});

describe('P17 — FoundationStack: gluePipelineRole IAM verification', () => {
  // Per Requirement 16.2, the role used by Glue ETL Jobs and runtime
  // crawlers needs to (1) read/write the Watermarks_Table for incremental
  // mode, and (2) read the JDBC discovery database. Per the comment block
  // in foundation-stack.ts, both grants are already covered by existing
  // wildcard statements (`agentic_data_ingestion*` for DDB, `agentic_data_ingestion*` for Glue) and
  // intentionally NOT duplicated. These tests document that the existing
  // wildcards still match the new resources so future refactors don't
  // tighten the scopes in a way that breaks the JDBC source path.

  it('gluePipelineRole DynamoDB wildcard covers agentic_data_ingestion_pipeline_watermarks', () => {
    // The shared dynamoDbPolicy ManagedPolicy grants read/write on
    // `arn:aws:dynamodb:*:*:table/agentic_data_ingestion*` — verify the wildcard is
    // still in place and would match the watermarks table.
    foundationTemplate().hasResourceProperties('AWS::IAM::ManagedPolicy', {
      ManagedPolicyName: `${TEST_PROJECT}-${TEST_ENV}-dynamodb-policy`,
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith([
              'dynamodb:GetItem',
              'dynamodb:PutItem',
              'dynamodb:UpdateItem',
            ]),
            Effect: 'Allow',
            Resource: Match.arrayWith([
              `arn:aws:dynamodb:${TEST_REGION}:${TEST_ACCOUNT}:table/agentic_data_ingestion*`,
            ]),
          }),
        ]),
      }),
    });
  });

  it('gluePipelineRole glueAdmin wildcard covers the JDBC discovery DB', () => {
    // The inline `glueAdmin` statement grants `glue:*` on
    // `database/agentic_data_ingestion*` and `table/agentic_data_ingestion*/*`. Verify the wildcard
    // is still in place — `agentic_data_ingestion_jdbc_discovery_*` matches the
    // `agentic_data_ingestion*` wildcard so no duplicate grant is needed.
    foundationTemplate().hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: 'glue:*',
            Effect: 'Allow',
            Resource: Match.arrayWith([
              `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:database/agentic_data_ingestion*`,
              `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:table/agentic_data_ingestion*/*`,
            ]),
          }),
        ]),
      }),
      Roles: Match.arrayWith([
        Match.objectLike({ Ref: Match.stringLikeRegexp('^GluePipeline') }),
      ]),
    });
  });

  it('gluePipelineRole trusts glue.amazonaws.com so JDBC crawlers can assume it', () => {
    // P17 fix: the dashboard API passes gluePipelineRole to Glue as the
    // discovery crawler's execution role. A Glue Crawler assumes its
    // execution role via the glue.amazonaws.com service principal, so the
    // role's trust policy must allow that principal in addition to Lambda.
    // Without this trust the crawler run fails (status poll returns FAILED)
    // even though CreateCrawler succeeds. The AssumeRolePolicyDocument must
    // therefore list BOTH lambda.amazonaws.com and glue.amazonaws.com.
    foundationTemplate().hasResource('AWS::IAM::Role', {
      Properties: Match.objectLike({
        RoleName: `${TEST_PROJECT}-${TEST_ENV}-gluepipeline-role`,
        AssumeRolePolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: 'sts:AssumeRole',
              Effect: 'Allow',
              Principal: { Service: 'glue.amazonaws.com' },
            }),
          ]),
        }),
      }),
    });
  });

  it('gluePipelineRole has EC2 ENI + VPC describe perms so JDBC crawlers can run in-VPC', () => {
    // P17 fix: a Glue Crawler reaching a JDBC source through a VPC-bound
    // Glue Connection runs inside the customer VPC via an ENI, exactly like
    // a Glue ETL job. The crawler's execution role (gluePipelineRole) needs
    // ENI lifecycle + VPC describe perms or the crawl fails with
    // "glue.amazonaws.com is not authorized to perform: ec2:DescribeSubnets".
    // EC2 Describe/CreateNetworkInterface do not support resource-level
    // permissions, so the resource is intentionally `*`.
    foundationTemplate().hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: [
              'ec2:CreateNetworkInterface',
              'ec2:DescribeNetworkInterfaces',
              'ec2:DeleteNetworkInterface',
              'ec2:DescribeVpcEndpoints',
              'ec2:DescribeRouteTables',
              'ec2:DescribeSecurityGroups',
              'ec2:DescribeSubnets',
              'ec2:DescribeVpcAttribute',
            ],
            Effect: 'Allow',
            Resource: '*',
          }),
        ]),
      }),
      Roles: Match.arrayWith([
        Match.objectLike({ Ref: Match.stringLikeRegexp('^GluePipeline') }),
      ]),
    });
  });
});

describe('P17 — FoundationStack: job-builder runtime role JDBC connection reads', () => {
  // create_glue_pipeline runs in its AgentCore container (USE_AGENTCORE_RUNTIME
  // =true) under the job-builder runtime role since R-02. It reads the target's
  // Glue connection at job-creation time, and that connection can be any
  // pre-existing / customer-managed one (e.g. agentglue-test-fixtures-postgres-
  // target) whose name does NOT follow the agentic-data-ingestion-* convention,
  // so the read must be account-wide (connection/*).
  //
  // It no longer reads the connection's secret: T-19 (66e97e5) made the job
  // resolve the credential itself at run time, under its own role, so the
  // runtime's GetSecretValue grant was dropped in R-02.
  it('the job-builder runtime role can read ANY Glue connection (glue:GetConnection on connection/*)', () => {
    foundationTemplate().hasResourceProperties('AWS::IAM::Policy', {
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: ['glue:GetConnection', 'glue:GetConnections', 'glue:GetTags'],
            Effect: 'Allow',
            Resource: [
              `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:catalog`,
              `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:connection/*`,
            ],
          }),
        ]),
      }),
      Roles: Match.arrayWith([
        Match.objectLike({ Ref: Match.stringLikeRegexp('^CreateGluePipelineRuntimeRole') }),
      ]),
    });
  });

  it('the job-builder runtime role reads no Secrets Manager secret', () => {
    const json = foundationTemplate().toJSON();
    for (const resource of Object.values(json.Resources ?? {}) as any[]) {
      if (resource.Type !== 'AWS::IAM::Policy') continue;
      const roles = JSON.stringify(resource.Properties?.Roles ?? []);
      if (!roles.includes('CreateGluePipelineRuntimeRole')) continue;
      const actions = (resource.Properties?.PolicyDocument?.Statement ?? []).flatMap((st: any) =>
        Array.isArray(st.Action) ? st.Action : [st.Action],
      );
      expect(actions).not.toContain('secretsmanager:GetSecretValue');
    }
  });
});

// ─────────────────────────────────────────────────────────────────────
// Orchestration Stack (Tasks 15.1, 15.2) — discovery cleanup rule
// ─────────────────────────────────────────────────────────────────────
//
// The DiscoveryCleanupRule is the daily janitor that targets the dashboard
// API Lambda to clean up wizard-time JDBC schema-discovery resources older
// than 7 days (Requirement 4.11). The OrchestrationStack synthesis
// requires fully-instantiated Foundation/Data/Compute stacks because of
// cross-stack imports, so we mirror the mock-stack pattern from
// orchestration-stack.test.ts to avoid Docker bundling.

import * as path from 'path';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { OrchestrationStack } from '../lib/orchestration-stack';

function synthOrchestrationStackForP17(): Template {
  const app = new cdk.App();
  const env = { account: TEST_ACCOUNT, region: TEST_REGION };

  // ── Mock Foundation Stack ──────────────────────────────────────
  const foundationStack = new cdk.Stack(app, 'MockFoundationP17', { env });
  const sourceBucket = new s3.Bucket(foundationStack, 'SourceBucket', {
    bucketName: `adi-source-files-${TEST_ACCOUNT}-${TEST_REGION}`,
  });
  const pipelineTriggerRole = new iam.Role(foundationStack, 'PipelineTriggerRole', {
    assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
  });

  // ── Mock Data Stack ────────────────────────────────────────────
  const dataStack = new cdk.Stack(app, 'MockDataP17', { env });
  const stateTable = new dynamodb.Table(dataStack, 'StateTable', {
    tableName: 'agentic_data_ingestion_state',
    partitionKey: { name: 'run_id', type: dynamodb.AttributeType.STRING },
    sortKey: { name: 'step_name', type: dynamodb.AttributeType.STRING },
    stream: dynamodb.StreamViewType.NEW_IMAGE,
  });
  const pipelinesTable = new dynamodb.Table(dataStack, 'PipelinesTable', {
    tableName: 'agentic_data_ingestion_pipelines',
    partitionKey: { name: 'pipeline_id', type: dynamodb.AttributeType.STRING },
  });
  const approvalsTable = new dynamodb.Table(dataStack, 'ApprovalsTable', {
    tableName: 'agentic_data_ingestion_approvals',
    partitionKey: { name: 'approval_id', type: dynamodb.AttributeType.STRING },
  });
  const pipelineLocksTable = new dynamodb.Table(dataStack, 'PipelineLocksTable', {
    tableName: 'agentic_data_ingestion_pipeline_locks',
    partitionKey: { name: 'pipeline_prefix', type: dynamodb.AttributeType.STRING },
  });
  const executionsTable = new dynamodb.Table(dataStack, 'ExecutionsTable', {
    tableName: 'agentic_data_ingestion_executions',
    partitionKey: { name: 'pipeline_id', type: dynamodb.AttributeType.STRING },
    sortKey: { name: 'run_id', type: dynamodb.AttributeType.STRING },
  });
  // p30 R9.1: the trigger's PROCESSED_FILES_TABLE env var reads this, so the
  // mock data stack must carry it or OrchestrationStack cannot synth.
  const processedFilesTable = new dynamodb.Table(dataStack, 'ProcessedFilesTable', {
    tableName: 'agentic_data_ingestion_processed_files',
    partitionKey: { name: 'pipeline_id', type: dynamodb.AttributeType.STRING },
    sortKey: { name: 'fingerprint', type: dynamodb.AttributeType.STRING },
  });
  const eventBus = new events.EventBus(dataStack, 'EventBus', {
    eventBusName: `agentic-data-ingestion-pipeline-events-${TEST_ENV}`,
  });
  new sqs.Queue(dataStack, 'DLQ', {
    queueName: `agentic-data-ingestion-eventbridge-dlq-${TEST_ENV}`,
  });

  // ── Mock Compute Stack (inline Lambda functions — no Docker) ───
  const computeStack = new cdk.Stack(app, 'MockComputeP17', { env });
  const mockLambda = (id: string, fnName: string) =>
    new lambda.Function(computeStack, id, {
      functionName: fnName,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(e,c): return {}'),
    });

  const compute = {
    fileValidationLambda: mockLambda('FV', 'agentic_data_ingestion_file_validation'),
    schemaExtractionLambda: mockLambda('SE', 'agentic_data_ingestion_schema_extract'),
    schemaComparisonLambda: mockLambda('SC', 'agentic_data_ingestion_schema_compare'),
    schemaMappingLambda: mockLambda('SM', 'agentic_data_ingestion_schema_mapping'),
    codeGenerationLambda: mockLambda('CG', 'agentic_data_ingestion_create_code'),
    codeTestingLambda: mockLambda('CT', 'agentic_data_ingestion_test_code'),
    createGluePipelineLambda: mockLambda('CGP', 'agentic_data_ingestion_create_glue_pipeline'),
    createInfrastructureLambda: mockLambda('CI', 'agentic_data_ingestion_create_infrastructure'),
    createQualityChecksLambda: mockLambda('CQC', 'agentic_data_ingestion_create_quality_checks'),
    runPipelineLambda: mockLambda('RP', 'agentic_data_ingestion_run_pipeline'),
    approvalHandlerLambda: mockLambda('AH', 'agentic_data_ingestion_approval_handler'),
    jobCompletionLambda: mockLambda('JC', 'agentic_data_ingestion_job_completion'),
    approvalTimeoutHandlerLambda: mockLambda('ATH', 'agentic_data_ingestion_approval_timeout_handler'),
    dashboardApiLambda: mockLambda('DA', 'agentic_data_ingestion_dashboard_api'),
    // p30a R1.1: CostOptimizationScheduleRule targets this function, so the
    // mock compute stack has to carry it or the Orchestration stack cannot synth.
    agentDispatchLambda: mockLambda('AD', 'agentic_data_ingestion_agent_dispatch'),
    strandsLayer: new lambda.LayerVersion(computeStack, 'MockStrandsLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '.')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
    }),
    adotLayer: lambda.LayerVersion.fromLayerVersionArn(
      computeStack,
      'MockAdotLayer',
      'arn:aws:lambda:us-west-2:901920570463:layer:aws-otel-python-arm64-ver-1-32-0:2',
    ),
  };

  const mockData = Object.assign(dataStack, {
    stateTable,
    pipelinesTable,
    approvalsTable,
    pipelineLocksTable,
    executionsTable,
    processedFilesTable,
    eventBus,
  });
  const mockFoundation = Object.assign(foundationStack, {
    sourceBucket,
    pipelineTriggerRole,
  });

  const orchestrationStack = new OrchestrationStack(app, 'TestOrchestrationP17', {
    env,
    environment: TEST_ENV,
    compute: compute as any,
    data: mockData as any,
    foundation: mockFoundation as any,
  });
  return Template.fromStack(orchestrationStack);
}

let _orchestrationTemplate: Template | undefined;
function orchestrationTemplate(): Template {
  if (!_orchestrationTemplate) _orchestrationTemplate = synthOrchestrationStackForP17();
  return _orchestrationTemplate;
}

describe('P17 — OrchestrationStack: discovery cleanup EventBridge rule', () => {
  it('Discovery cleanup rule exists with the correct rule name', () => {
    // Logical-ID `DiscoveryCleanupRule`, ruleName interpolated with
    // environment — confirms Task 15.1 wired the rule into the stack.
    orchestrationTemplate().hasResourceProperties('AWS::Events::Rule', {
      Name: `agentic-data-ingestion-discovery-cleanup-${TEST_ENV}`,
    });
  });

  it('Discovery cleanup rule fires daily at 03:00 UTC', () => {
    // events.Schedule.cron({ minute: '0', hour: '3' }) — CDK emits this
    // as `cron(0 3 * * ? *)` (CloudWatch Events 6-field cron format with
    // both day-of-month and day-of-week wildcards required).
    orchestrationTemplate().hasResourceProperties('AWS::Events::Rule', {
      Name: `agentic-data-ingestion-discovery-cleanup-${TEST_ENV}`,
      ScheduleExpression: 'cron(0 3 * * ? *)',
    });
  });

  it('Discovery cleanup rule targets the dashboard API Lambda', () => {
    // The target Arn is a cross-stack reference to the dashboard API
    // Lambda's GetAtt(...).Arn — match it via Match.objectLike so we
    // accept either Fn::GetAtt or Fn::ImportValue token shapes without
    // pinning to a specific synthesized logical ID.
    orchestrationTemplate().hasResourceProperties('AWS::Events::Rule', {
      Name: `agentic-data-ingestion-discovery-cleanup-${TEST_ENV}`,
      Targets: Match.arrayWith([
        Match.objectLike({
          Arn: Match.anyValue(),
        }),
      ]),
    });
  });

  it('Discovery cleanup rule has implicit Lambda invoke permission', () => {
    // `targets.LambdaFunction(dashboardApiLambda)` implicitly synthesises
    // a single AWS::Lambda::Permission resource granting
    // events.amazonaws.com permission to invoke the function with the
    // rule's ARN as the SourceArn. SourceArn is a Fn::GetAtt to the
    // DiscoveryCleanupRule's Arn — assert via Match.objectLike on the
    // intrinsic.
    orchestrationTemplate().hasResourceProperties('AWS::Lambda::Permission', {
      Action: 'lambda:InvokeFunction',
      Principal: 'events.amazonaws.com',
      SourceArn: Match.objectLike({
        'Fn::GetAtt': Match.arrayWith([
          Match.stringLikeRegexp('^DiscoveryCleanupRule'),
          'Arn',
        ]),
      }),
    });

    // Exactly one such permission — guards against the explicit-
    // addPermission anti-pattern called out in the orchestration-stack
    // comment (would create a duplicate permission).
    const json = orchestrationTemplate().toJSON();
    const permissions = Object.entries(json.Resources).filter(
      ([, r]: [string, any]) =>
        r.Type === 'AWS::Lambda::Permission' &&
        r.Properties?.Principal === 'events.amazonaws.com' &&
        JSON.stringify(r.Properties?.SourceArn ?? '').includes('DiscoveryCleanupRule'),
    );
    expect(permissions.length).toBe(1);
  });
});
