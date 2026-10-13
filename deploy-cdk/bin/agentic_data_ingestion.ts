#!/usr/bin/env node
// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { FoundationStack } from '../lib/foundation-stack';
import { DataStack } from '../lib/data-stack';
import { ComputeStack } from '../lib/compute-stack';
import { OrchestrationStack } from '../lib/orchestration-stack';
import { AgentCoreStack } from '../lib/agentcore-stack';
import { AuthStack } from '../lib/auth-stack';
import { HostingStack } from '../lib/hosting-stack';
import { EdgeWafStack } from '../lib/edge-waf-stack';
import { TestFixturesStack } from '../lib/test-fixtures-stack';
import { resolveEnv } from '../lib/resolve-env';
import { applyNagAcknowledgments } from '../lib/nag-acknowledgments';

const app = new cdk.App();

// AWS Solutions security/best-practice checks run on every synth (cdk-nag v3
// registers as a policy validation plugin via the Validations API). Findings
// are either fixed or suppressed with a written justification — see
// docs/CDK-NAG.md for the suppression inventory. Disable locally with
// `-c adi:skipNag=true` (e.g. for fast iteration).
if (app.node.tryGetContext('adi:skipNag') !== 'true') {
  cdk.Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));

  // Acknowledged findings. Every entry has a written justification and is
  // inventoried in docs/CDK-NAG.md. Transport-security and observability
  // findings (S10, SQS4, SNS3, SF1, SF2) were FIXED, not acknowledged.
  //
  // NOTE: IAM4/IAM5 are NOT acknowledged here. Those rules emit GRANULAR
  // finding IDs (e.g. 'AwsSolutions-IAM5[Resource::arn:...]') that a
  // whole-rule acknowledgment can never match — and Validations.acknowledge()
  // rejects granular IDs outright (their '[...::...]' payload trips the '::'
  // prefix check in qualifyId). They are covered per stack by the GENERATED
  // applyNagAcknowledgments() call below (see lib/nag-acknowledgments.ts).
  cdk.Validations.of(app).acknowledge({
    id: 'AwsSolutions::AwsSolutions-DDB3',
    reason:
      'Point-in-time recovery is omitted on the operational task-token/WS-connection/error ' +
      'tables to keep sample cost low; the data is transient and reconstructible. Enable ' +
      'PITR for production workloads.',
  });
  cdk.Validations.of(app).acknowledge({
    id: 'AwsSolutions::AwsSolutions-CFR1',
    reason:
      'Geo restrictions are a per-deployment business decision; the sample ships without ' +
      'them. The distribution is protected by the CLOUDFRONT-scope WAF (managed rules + ' +
      'rate limiting) and Cognito authentication.',
  });
  cdk.Validations.of(app).acknowledge({
    id: 'AwsSolutions::AwsSolutions-L1',
    reason:
      'Lambda runtime is pinned to Python 3.12 to match the Strands Agents layer build ' +
      'and the ADOT auto-instrumentation layer compatibility matrix.',
  });
  cdk.Validations.of(app).acknowledge({
    id: 'AwsSolutions::AwsSolutions-S1',
    reason:
      'S3 server access logging is omitted to keep sample cost low. CloudTrail data ' +
      'events can be enabled for object-level auditing in production.',
  });
  cdk.Validations.of(app).acknowledge({
    id: 'AwsSolutions::AwsSolutions-SQS3',
    reason: 'The flagged queues are themselves dead-letter queues; a DLQ chain is not required.',
  });
  cdk.Validations.of(app).acknowledge({
    id: 'AwsSolutions::AwsSolutions-APIG1',
    reason:
      'The REST stage has access logging (one-month retention). The WebSocket stage ' +
      'does not: its $connect token travels in the query string, so an access log ' +
      'there would record live ID tokens. Connections are observable through the ' +
      'authorizer and connect Lambda logs.',
  });
  cdk.Validations.of(app).acknowledge({
    id: 'AwsSolutions::AwsSolutions-APIG2',
    reason:
      'Request validation is performed in the dashboard API router Lambda, which returns ' +
      'structured 4xx responses; schema-level validation at the gateway is redundant here.',
  });
  cdk.Validations.of(app).acknowledge({
    id: 'AwsSolutions::AwsSolutions-APIG4',
    reason:
      'The WebSocket $disconnect/$default routes rely on connection-scoped authorization: ' +
      'the $connect route has a Lambda authorizer validating Cognito JWTs, and API Gateway ' +
      'only invokes later routes on connections that passed it.',
  });
  cdk.Validations.of(app).acknowledge({
    id: 'AwsSolutions::AwsSolutions-CFR4',
    reason:
      'The distribution uses the default CloudFront certificate, which cannot enforce a ' +
      'TLS 1.2 minimum viewer policy. Attach a custom domain + ACM certificate in ' +
      'production to raise the minimum protocol version.',
  });
  cdk.Validations.of(app).acknowledge({
    id: 'AwsSolutions::AwsSolutions-COG2',
    reason:
      'MFA (TOTP) is required by default. It is optional only when the e2e test client ' +
      'is deployed (-c adi:deployE2eClient=true), whose USER_PASSWORD_AUTH sign-in ' +
      'cannot answer an MFA_SETUP challenge, or when -c adi:requireMfa=false is passed ' +
      'deliberately (see README "Hardening for real data").',
  });
  cdk.Validations.of(app).acknowledge({
    id: 'AwsSolutions::AwsSolutions-COG8',
    reason:
      'The Cognito Plus feature plan adds threat protection at additional cost; the sample ' +
      'defaults to the Essentials tier. Upgrade to Plus for production workloads.',
  });
  cdk.Validations.of(app).acknowledge({
    id: 'AwsSolutions::AwsSolutions-SMG4',
    reason:
      'The X-Origin-Verify secret requires a coordinated CloudFront origin-header update ' +
      'on rotation; automatic rotation without that orchestration would break API access. ' +
      'Manual rotation is documented in the deployment guide.',
  });
}

const env = app.node.tryGetContext('environment') ?? 'dev';
const projectName = app.node.tryGetContext('projectName') ?? 'agentic-data-ingestion';

// Fail fast with actionable messages when account/region are unresolved
// (P27 Req 7). Must run before any stack construction: the Auth stack embeds
// the concrete account ID in its Cognito domain prefix (P27 Req 5.4).
const awsEnv: cdk.Environment = resolveEnv();

// Stack 1: Foundation (IAM + ECR + S3)
const foundation = new FoundationStack(app, `AgenticDataIngestion-Foundation-${env}`, {
  env: awsEnv,
  projectName,
  environment: env,
});

// Stack 2: Data (DynamoDB + EventBridge)
const data = new DataStack(app, `AgenticDataIngestion-Data-${env}`, {
  env: awsEnv,
  environment: env,
});

// The WebSocket authorizer (Compute) accepts tokens from the app clients whose
// IDs these SSM parameters hold (R-05). Hosting writes the dashboard client's
// and Auth writes the e2e client's when it exists; both deploy around Compute
// rather than before it, so the IDs travel through SSM, not stack references.
const dashboardClientIdParameterName = `/agentic-data-ingestion/${env}/dashboard-client-id`;
const e2eClientIdParameterName = `/agentic-data-ingestion/${env}/e2e-client-id`;

// Stack 3: Auth (Cognito User Pool) — no dependencies
const authStack = new AuthStack(app, `AgenticDataIngestion-Auth-${env}`, {
  env: awsEnv,
  environment: env,
  e2eClientIdParameterName,
});

// Stack 4: AgentCore (MCP runtimes) — depends only on Foundation
const agentcore = new AgentCoreStack(app, `AgenticDataIngestion-AgentCore-${env}`, {
  env: awsEnv,
  environment: env,
  foundation,
});
agentcore.addDependency(foundation);

// Stack 5: Compute (Lambda + API Gateway) — depends on Foundation, Data, AgentCore, Auth
// Use explicit Fn.importValue with stable export names to avoid CDK's
// auto-generated cross-stack exports that cause deadlocks on updates.
const agentCoreStackName = `AgenticDataIngestion-AgentCore-${env}`;

const compute = new ComputeStack(app, `AgenticDataIngestion-Compute-${env}`, {
  env: awsEnv,
  environment: env,
  projectName,
  foundation,
  data,
  userPoolArn: authStack.userPool.userPoolArn,
  wsClientIdParameterNames: [dashboardClientIdParameterName, e2eClientIdParameterName],
  agentCoreEndpoint: cdk.Fn.importValue(`${agentCoreStackName}-InvocationURL`),
  agentCoreS3TablesEndpoint: cdk.Fn.importValue(`${agentCoreStackName}-S3TablesInvocationURL`),
  agentCoreCodegenEndpoint: cdk.Fn.importValue(`${agentCoreStackName}-CodegenInvocationURL`),
  agentCoreInfraEndpoint: cdk.Fn.importValue(`${agentCoreStackName}-InfraInvocationURL`),
  agentCoreAnalyticsEndpoint: cdk.Fn.importValue(`${agentCoreStackName}-AnalyticsInvocationURL`),
  agentCoreMappingMemoryId: cdk.Fn.importValue(`${agentCoreStackName}-MappingMemoryId`),
  agentCoreCreateCodeEndpoint: cdk.Fn.importValue(`${agentCoreStackName}-CreateCodeAgentEndpoint`),
  agentCoreTestCodeEndpoint: cdk.Fn.importValue(`${agentCoreStackName}-TestCodeAgentEndpoint`),
  agentCoreCreatePipelineEndpoint: cdk.Fn.importValue(`${agentCoreStackName}-CreateGluePipelineAgentEndpoint`),
  agentCoreCreateInfraEndpoint: cdk.Fn.importValue(`${agentCoreStackName}-CreateInfraAgentEndpoint`),
  agentCoreSelfHealingEndpoint: cdk.Fn.importValue(`${agentCoreStackName}-SelfHealingAgentEndpoint`),
  agentCoreComplexMappingEndpoint: cdk.Fn.importValue(`${agentCoreStackName}-ComplexMappingAgentEndpoint`),
  agentCoreDataQualityEndpoint: cdk.Fn.importValue(`${agentCoreStackName}-DataQualityAgentEndpoint`),
  agentCoreCostOptimizationEndpoint: cdk.Fn.importValue(`${agentCoreStackName}-CostOptimizationAgentEndpoint`),
  agentCoreNlPipelineBuilderEndpoint: cdk.Fn.importValue(`${agentCoreStackName}-NLPipelineBuilderAgentEndpoint`),
});
compute.addDependency(foundation);
compute.addDependency(data);
compute.addDependency(agentcore);
compute.addDependency(authStack);

// Stack 6: Orchestration (Step Functions + EventBridge rules + CloudWatch)
// `notificationEmail` was previously read by nobody: cdk.json carried a value,
// the stack fell back to a hardcoded placeholder, and the `-c notificationEmail=`
// override documented in both READMEs was silently ignored. Threaded through here
// so the documented override actually works; unset means no email subscription.
const orchestration = new OrchestrationStack(app, `AgenticDataIngestion-Orchestration-${env}`, {
  env: awsEnv,
  environment: env,
  notificationEmail: app.node.tryGetContext('notificationEmail'),
  compute,
  data,
  foundation,
});
orchestration.addDependency(compute);
orchestration.addDependency(data);
orchestration.addDependency(foundation);

// Stack 7: Edge WAF (CLOUDFRONT-scope WAF) — pinned to us-east-1.
// A CLOUDFRONT-scope WebACL must be created in us-east-1, but the Hosting
// stack deploys to the application region. The ARN is wired into Hosting via
// cross-region references.
const edgeWaf = new EdgeWafStack(app, `AgenticDataIngestion-EdgeWaf-${env}`, {
  env: { account: awsEnv.account, region: 'us-east-1' },
  environment: env,
  crossRegionReferences: true,
});

// Stack 8: Hosting (CloudFront + S3 + Cognito App Client) — depends on Compute, Auth & EdgeWaf
const hosting = new HostingStack(app, `AgenticDataIngestion-Hosting-${env}`, {
  env: awsEnv,
  environment: env,
  crossRegionReferences: true,
  restApiHost: `${compute.api.restApiId}.execute-api.${awsEnv.region}.amazonaws.com`,
  userPoolId: authStack.userPool.userPoolId,
  userPoolArn: authStack.userPool.userPoolArn,
  hostedUiDomain: authStack.userPoolDomain.baseUrl(),
  wsUrl: compute.wsApiEndpoint,
  originVerifySecretValue: compute.originVerifySecret?.secretValue.unsafeUnwrap(),
  dashboardClientIdParameterName,
  cloudFrontWebAclArn: edgeWaf.webAclArn,
});
hosting.addDependency(compute);
hosting.addDependency(authStack);
hosting.addDependency(edgeWaf);

// Opt-in Stack: TestFixtures (JDBC e2e test databases) — gated by the
// `adi:deployTestFixtures` context flag. A plain `cdk deploy --all`
// does NOT instantiate this stack (gate defaults off), preserving the
// current 5-stack behavior. The stack reads `adi:skipGlueConnections`
// from context internally, so no extra prop is needed here.
if (
  app.node.tryGetContext('adi:deployTestFixtures') === 'true' ||
  app.node.tryGetContext('adi:deployTestFixtures') === true
) {
  new TestFixturesStack(app, `AgenticDataIngestion-TestFixtures-${env}`, {
    env: awsEnv,
    environment: env,
  });
}

// Granular cdk-nag acknowledgments (IAM4/IAM5 per-finding IDs) — GENERATED
// module, regenerate via scripts/generate_nag_acknowledgments.py when
// findings change (procedure in docs/CDK-NAG.md). Applied unconditionally:
// writing metadata is harmless when nag is skipped.
applyNagAcknowledgments(
  {
    foundation,
    data,
    auth: authStack,
    agentcore,
    compute,
    orchestration,
    edgeWaf,
    hosting,
  },
  env,
);

app.synth();
