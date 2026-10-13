// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { HostingStack } from '../lib/hosting-stack';
import { EdgeWafStack } from '../lib/edge-waf-stack';

/**
 * CDK assertion tests for the Dashboard Hosting + Cognito Auth feature.
 *
 * Validates: Requirements 1.1, 1.2, 1.3, 2.1, 2.2, 4.1, 5.1, 8.1, 10.1, 10.2
 *
 * Synthesis details:
 *   - Docker bundling is suppressed (`aws:cdk:bundling-stacks` = []) so
 *     Lambda layers and the dashboard asset don't require Docker or a build.
 *   - Account/region pinned to a synthetic test env (111111111111 / us-west-2).
 *   - HostingStack is synthesized WITH a synthetic `userPoolArn`: since P27
 *     the dashboard asset points at the dashboard-react SOURCE directory
 *     (auto-built via bundling), so no pre-built dist/ is required and the
 *     UserPoolClient + BucketDeployment resources are assertable.
 *   - ComputeStack assertions (4.1, 5.1, 10.2) are validated via source-level
 *     inspection because the ComputeStack (20+ Lambdas) exceeds test memory
 *     limits during synth. A full `cdk synth` confirms these in CI.
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';

function makeApp(): cdk.App {
  return new cdk.App({
    context: { 'aws:cdk:bundling-stacks': [] },
  });
}

// =====================================================================
// HostingStack assertions (S3, CloudFront, WebACLId reference)
// Reqs: 1.1, 1.2, 1.3, 2.1, 2.2, 10.1 (WebACLId reference), 10.2 (origin header)
// The CLOUDFRONT-scope WAF resource itself now lives in EdgeWafStack
// (us-east-1) — see the dedicated describe block below.
// =====================================================================

describe('HostingStack — S3, CloudFront, API behavior', () => {
  let template: Template;
  let json: any;

  beforeAll(() => {
    const app = makeApp();
    const env = { account: TEST_ACCOUNT, region: TEST_REGION };

    const stack = new HostingStack(app, `AgenticDataIngestion-Hosting-${TEST_ENV}`, {
      env,
      environment: TEST_ENV,
      restApiHost: 'abc123.execute-api.us-west-2.amazonaws.com',
      originVerifySecretValue: 'test-secret-value-for-cdk-assertions',
      cloudFrontWebAclArn: 'arn:aws:wafv2:us-east-1:123456789012:global/webacl/test/abc',
      userPoolId: 'us-west-2_TestPool',
      userPoolArn: `arn:aws:cognito-idp:${TEST_REGION}:${TEST_ACCOUNT}:userpool/us-west-2_TestPool`,
      hostedUiDomain: 'https://agentic-data-ingestion-111111111111-dev.auth.us-west-2.amazoncognito.com',
      wsUrl: 'wss://ws123.execute-api.us-west-2.amazonaws.com/prod',
    });

    template = Template.fromStack(stack);
    json = template.toJSON();
  });

  // ── Requirement 1.1 — S3 bucket has BlockPublicAccess: BLOCK_ALL ──
  it('S3 bucket has all public access blocked', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
  });

  // ── publication-cleanup R11 — bucket name is account+region scoped ──
  // S3 bucket names are globally unique across ALL accounts; a deterministic
  // name without the account ID blocks every account after the first deployer.
  it('site bucket name embeds account and region (global-uniqueness guard)', () => {
    template.hasResourceProperties('AWS::S3::Bucket', {
      BucketName: `adi-dashboard-${TEST_ACCOUNT}-${TEST_REGION}`,
    });
  });

  // ── Requirement 1.2 — CloudFront default S3 behavior ──────────────
  it('distribution has a default S3 behavior with REDIRECT_TO_HTTPS', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        DefaultCacheBehavior: Match.objectLike({
          ViewerProtocolPolicy: 'redirect-to-https',
        }),
      }),
    });
  });

  // ── Requirement 1.3 — SPA fallback (REVERSED: no longer errorResponses) ────
  //
  // AMENDED IN PLACE, NOT DELETED, so the reversal stays legible as a reversal
  // rather than reading as a lost assertion — the convention `LGR-60c4985-6` and
  // `LGR-0fe3e5d-3` (both **template / ratified**) establish for a deliberate
  // change of direction.
  //
  // WHAT THIS USED TO ASSERT, and why it was wrong. Req 1.3 was satisfied by
  // `errorResponses` on the Distribution mapping 403 and 404 to HTTP 200 with the
  // body of `/index.html`, and this test pinned exactly that. CloudFront custom
  // error responses are a property of `DistributionConfig` and CANNOT be scoped to
  // a cache behaviour, so that mapping also rewrote every error the `/api/*`
  // behaviour produced: a real `404 no test results yet`
  // (`handlers/pipelines_artifacts.py:89`) reached the browser as HTML under a 200,
  // where `api-client._unwrap` (`services/api-client.ts:190-194`) could only report
  // it as `ApiError(200, 'Response body could not be read as JSON')`. 403 took the
  // same path, so WAF blocks and Cognito authorizer rejections were also laundered
  // into non-JSON 200s.
  //
  // Req 1.3's INTENT — SPA deep links resolve to index.html — is unchanged and is
  // now met by a viewer-request CloudFront Function attached to the default (S3)
  // behaviour only (`lib/spa-router-function.ts`). The behaviour of that function,
  // its attachment, its non-attachment to `/api/*`, and the absence of
  // `CustomErrorResponses` are all owned by `test/p56-spa-router-function.test.ts`.
  // The assertion below is the minimal restatement here so this suite's Req 1.3
  // coverage does not silently vanish.
  it('distribution has NO SPA error responses — the fallback moved to a CloudFront Function', () => {
    const distConfig = (
      Object.values(json.Resources).find(
        (r: any) => r.Type === 'AWS::CloudFront::Distribution',
      ) as any
    ).Properties.DistributionConfig;

    expect(
      distConfig.CustomErrorResponses,
      'CustomErrorResponses is back. It cannot be scoped per behaviour, so it re-breaks every ' +
        '/api/* 403 and 404 — see the comment above this test and ' +
        'test/p56-spa-router-function.test.ts for the full chain.',
    ).toBeUndefined();

    // And the replacement is actually present, so this is a move rather than a
    // removal.
    expect(
      distConfig.DefaultCacheBehavior?.FunctionAssociations,
      'the SPA fallback is gone entirely: no errorResponses AND no function association on the ' +
        'default behaviour. Extensionless routes (/, /pipelines, /auth/callback) now 403 from S3.',
    ).toBeDefined();
  });

  // ── Requirement 2.1 — /api/* behavior with caching disabled ───────
  it('distribution has a /api/* cache behavior', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        CacheBehaviors: Match.arrayWith([
          Match.objectLike({
            PathPattern: '/api/*',
            ViewerProtocolPolicy: 'redirect-to-https',
          }),
        ]),
      }),
    });
  });

  it('/api/* references a CachePolicyId and OriginRequestPolicyId (managed policies)', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        CacheBehaviors: Match.arrayWith([
          Match.objectLike({
            PathPattern: '/api/*',
            CachePolicyId: Match.anyValue(),
            OriginRequestPolicyId: Match.anyValue(),
          }),
        ]),
      }),
    });
  });

  // ── P27 Req 6.4 — dashboard deployment synthesizes without a pre-built dist ──
  // The asset targets the dashboard-react SOURCE directory (auto-built via
  // bundling, suppressed in tests), so these resources are now assertable.
  it('creates a Cognito app client with authorization-code flow and no localhost callbacks by default', () => {
    template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      AllowedOAuthFlows: ['code'],
      GenerateSecret: false,
    });
    // R-12: the localhost callback and logout URLs exist only with
    // -c adi:allowLocalhost=true (asserted in p47-cors-posture.test.ts and below).
    const client = Object.values(json.Resources).find((r: any) => r.Type === 'AWS::Cognito::UserPoolClient') as any;
    expect(JSON.stringify(client.Properties.CallbackURLs)).not.toContain('localhost');
    expect(JSON.stringify(client.Properties.LogoutURLs)).not.toContain('localhost');
  });

  // Security review S7: no username enumeration, and token lifetimes set
  // explicitly instead of Cognito's 30-day refresh-token default.
  it('the dashboard app client prevents user-existence errors and sets explicit token lifetimes', () => {
    template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      PreventUserExistenceErrors: 'ENABLED',
      AccessTokenValidity: 60,
      IdTokenValidity: 60,
      RefreshTokenValidity: 720,
      TokenValidityUnits: { AccessToken: 'minutes', IdToken: 'minutes', RefreshToken: 'minutes' },
    });
  });

  // Security review S9: CloudFront access logs to a private, TLS-only bucket
  // with 30-day expiry, named outside the adi-* family the Lambda roles can
  // read and write.
  it('CloudFront writes access logs to a private, TLS-only, 30-day bucket outside the adi-* grant family', () => {
    const distribution = Object.values(json.Resources).find(
      (r: any) => r.Type === 'AWS::CloudFront::Distribution',
    ) as any;
    const logging = distribution.Properties.DistributionConfig.Logging;
    expect(logging).toBeDefined();
    expect(logging.IncludeCookies).toBe(false);
    expect(logging.Prefix).toBe('cloudfront/');
    const bucketLogicalId = logging.Bucket['Fn::GetAtt'][0];
    const bucket = json.Resources[bucketLogicalId];
    expect(bucket.Type).toBe('AWS::S3::Bucket');
    expect(bucket.Properties.BucketName).toBe(`agentic-data-ingestion-cf-logs-${TEST_ACCOUNT}-${TEST_REGION}`);
    expect(bucket.Properties.BucketName).not.toMatch(/^adi-/);
    expect(bucket.Properties.PublicAccessBlockConfiguration).toEqual({
      BlockPublicAcls: true,
      BlockPublicPolicy: true,
      IgnorePublicAcls: true,
      RestrictPublicBuckets: true,
    });
    expect(bucket.Properties.OwnershipControls.Rules[0].ObjectOwnership).toBe('BucketOwnerPreferred');
    expect(JSON.stringify(bucket.Properties.LifecycleConfiguration)).toContain('"ExpirationInDays":30');
    const policy = Object.values(json.Resources).find(
      (r: any) => r.Type === 'AWS::S3::BucketPolicy' && JSON.stringify(r.Properties.Bucket).includes(bucketLogicalId),
    ) as any;
    expect(JSON.stringify(policy.Properties.PolicyDocument)).toContain('aws:SecureTransport');
  });

  it('adds the localhost callback and logout URLs with -c adi:allowLocalhost=true', () => {
    const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [], 'adi:allowLocalhost': 'true' } });
    const stack = new HostingStack(app, `AgenticDataIngestion-Hosting-${TEST_ENV}`, {
      env: { account: TEST_ACCOUNT, region: TEST_REGION },
      environment: TEST_ENV,
      userPoolId: 'us-west-2_TestPool',
      userPoolArn: `arn:aws:cognito-idp:${TEST_REGION}:${TEST_ACCOUNT}:userpool/us-west-2_TestPool`,
    });
    Template.fromStack(stack).hasResourceProperties('AWS::Cognito::UserPoolClient', {
      CallbackURLs: Match.arrayWith(['http://localhost:5173/auth/callback']),
      LogoutURLs: Match.arrayWith(['http://localhost:5173']),
    });
  });

  it('creates the dashboard BucketDeployment (auto-built dashboard + config.json)', () => {
    template.resourceCountIs('Custom::CDKBucketDeployment', 1);
  });

  // ── Requirement 10.1 — distribution references the CLOUDFRONT WAF ──
  // The WAF resource itself is asserted in the EdgeWafStack block below;
  // here we only assert the distribution wires up the WebACLId.
  it('the CloudFront distribution has a WebACLId set', () => {
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        WebACLId: Match.anyValue(),
      }),
    });
  });

  // ── Requirement 10.2 — /api/* origin sets X-Origin-Verify ──────────
  it('/api/* origin sets the X-Origin-Verify secret header', () => {
    const distResources = Object.values(json.Resources).filter(
      (r: any) => r.Type === 'AWS::CloudFront::Distribution',
    );
    expect(distResources.length).toBe(1);

    const distConfig = (distResources[0] as any).Properties.DistributionConfig;
    const apiOrigin = distConfig.Origins.find(
      (o: any) => o.OriginPath === '/v1',
    );
    expect(apiOrigin).toBeDefined();
    expect(apiOrigin.OriginCustomHeaders).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          HeaderName: 'X-Origin-Verify',
          HeaderValue: 'test-secret-value-for-cdk-assertions',
        }),
      ]),
    );
  });
});

// =====================================================================
// EdgeWafStack assertions (CLOUDFRONT-scope WAF, us-east-1)
// Req: 10.1 — the CLOUDFRONT-scope WAF web ACL with managed rule sets and
// a rate-based rule. Must be created in us-east-1 (separate stack), then
// wired into the Hosting distribution via cross-region references.
// =====================================================================

describe('EdgeWafStack — CLOUDFRONT-scope WAF (us-east-1)', () => {
  let template: Template;

  beforeAll(() => {
    const app = makeApp();

    const stack = new EdgeWafStack(app, `AgenticDataIngestion-EdgeWaf-${TEST_ENV}`, {
      env: { account: TEST_ACCOUNT, region: 'us-east-1' },
      environment: TEST_ENV,
    });

    template = Template.fromStack(stack);
  });

  // ── Requirement 10.1 — CLOUDFRONT-scope WAF web ACL ────────────────
  it('a CLOUDFRONT-scope WAF web ACL exists', () => {
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Scope: 'CLOUDFRONT',
      DefaultAction: { Allow: {} },
    });
  });

  // ── Requirement 10.1 — managed rule sets + rate-based rule ─────────
  it('the WAF web ACL has the three expected rules', () => {
    template.hasResourceProperties('AWS::WAFv2::WebACL', {
      Scope: 'CLOUDFRONT',
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: 'AWSManagedRulesCommonRuleSet',
          Priority: 1,
          Statement: Match.objectLike({
            ManagedRuleGroupStatement: Match.objectLike({
              VendorName: 'AWS',
              Name: 'AWSManagedRulesCommonRuleSet',
            }),
          }),
        }),
        Match.objectLike({
          Name: 'AWSManagedRulesKnownBadInputsRuleSet',
          Priority: 2,
          Statement: Match.objectLike({
            ManagedRuleGroupStatement: Match.objectLike({
              VendorName: 'AWS',
              Name: 'AWSManagedRulesKnownBadInputsRuleSet',
            }),
          }),
        }),
        Match.objectLike({
          Name: 'RateLimitPerIP',
          Priority: 3,
          Statement: Match.objectLike({
            RateBasedStatement: Match.objectLike({
              Limit: 2000,
              AggregateKeyType: 'IP',
            }),
          }),
        }),
      ]),
    });
  });
});

// =====================================================================
// ComputeStack — source-level verification for Cognito authorizer,
// WS authorizer, and REGIONAL WAF.
//
// The full ComputeStack (20+ Lambdas with Code.fromAsset) exceeds vitest
// worker memory during synth. Instead of Template assertions, we verify
// the constructs are defined correctly by inspecting the source.
// Full synth validation is done via `cdk synth` in CI.
//
// Reqs: 4.1, 5.1, 10.2
// =====================================================================

describe('ComputeStack — source verification (4.1, 5.1, 10.2)', () => {
  let source: string;

  beforeAll(async () => {
    const { readFileSync } = await import('fs');
    const { join } = await import('path');
    source = readFileSync(join(__dirname, '..', 'lib', 'compute-stack.ts'), 'utf-8');
  });

  // Requirement 4.1 — REST API uses Cognito authorizer when userPoolArn provided
  it('compute-stack defines a CognitoUserPoolsAuthorizer when userPoolArn is set', () => {
    expect(source).toContain('CognitoUserPoolsAuthorizer');
    expect(source).toContain('authorizationType: apigateway.AuthorizationType.COGNITO');
  });

  // Requirement 5.1 — WebSocket $connect has a Lambda authorizer
  it('compute-stack defines a WebSocketLambdaAuthorizer on $connect', () => {
    expect(source).toContain('WebSocketLambdaAuthorizer');
    expect(source).toContain('identitySource');
    expect(source).toContain('route.request.querystring.token');
  });

  // Requirement 10.2 — REGIONAL WAF with x-origin-verify byte-match
  it('compute-stack defines a REGIONAL WAF with x-origin-verify byte-match rule', () => {
    expect(source).toContain("scope: 'REGIONAL'");
    // `Name`, the key the AWS::WAFv2::WebACL schema requires; CDK passes this
    // `any`-typed property through verbatim, so the source spelling is the
    // template spelling.
    expect(source).toContain("singleHeader: { Name: 'x-origin-verify' }");
    expect(source).toContain("positionalConstraint: 'EXACTLY'");
  });

  // Requirements 10.2, 10.3, 10.4 — WebACLAssociation targets the API stage AND
  // declares an explicit dependency on it (create-time ordering fix, task 18).
  // Deriving resourceArn from restApiId (a token) only infers a dependency on the
  // RestApi, not the 'v1' stage, so a fresh create can hit WAFv2 NotFound.
  it('compute-stack associates the regional WAF with the API stage and depends on it', () => {
    expect(source).toContain('CfnWebACLAssociation');
    // resourceArn uses the stage's ARN token (not a hand-built restapis/stages ARN)
    expect(source).toContain('resourceArn: this.api.deploymentStage.stageArn');
    // explicit dependency on the deployment stage guards the WAFv2 NotFound create race
    expect(source).toContain(
      'apiWafAssociation.node.addDependency(this.api.deploymentStage)',
    );
  });
});

// =====================================================================
// ComputeStack — REGIONAL WAF association create-time ordering (task 18)
//
// The real ComputeStack (20+ Lambdas with Code.fromAsset) exceeds vitest
// worker memory during synth (see the source-verification block above), so
// this reproduces the EXACT association pattern used in compute-stack.ts's
// `if (props.userPoolArn)` edge-hardening block in a minimal synthesizable
// stack. This asserts the CloudFormation-level behavior — that the
// association carries a `DependsOn` on the API Gateway stage — which a
// source-text check cannot. The source-verification test above guards that
// compute-stack.ts actually uses this pattern.
//
// Validates: Requirements 10.3, 10.4
// =====================================================================

describe('ComputeStack — REGIONAL WAF association DependsOn stage (10.3, 10.4)', () => {
  let template: Template;

  beforeAll(() => {
    const app = makeApp();
    const stack = new cdk.Stack(app, 'MockWafAssocStack', {
      env: { account: TEST_ACCOUNT, region: TEST_REGION },
    });

    // Mirror compute-stack.ts: a REGIONAL REST API with a 'v1' deployment stage.
    const api = new apigateway.RestApi(stack, 'DashboardRestApi', {
      endpointTypes: [apigateway.EndpointType.REGIONAL],
      deployOptions: { stageName: 'v1' },
    });
    // A method is required for the deployment/stage to synthesize.
    api.root.addMethod('ANY');

    const apiWafAcl = new wafv2.CfnWebACL(stack, 'ApiRegionalWaf', {
      scope: 'REGIONAL',
      defaultAction: { block: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: 'test-api-regional-waf',
        sampledRequestsEnabled: true,
      },
    });

    // The exact pattern under test (identical to compute-stack.ts):
    const apiWafAssociation = new wafv2.CfnWebACLAssociation(stack, 'ApiWafAssociation', {
      webAclArn: apiWafAcl.attrArn,
      resourceArn: api.deploymentStage.stageArn,
    });
    apiWafAssociation.node.addDependency(api.deploymentStage);

    template = Template.fromStack(stack);
  });

  // ── Requirements 10.3, 10.4 — association must depend on the API stage so a
  // fresh create never runs before the 'v1' stage exists (WAFv2 NotFound race).
  it('WebACLAssociation has a DependsOn referencing the API Gateway stage logical id', () => {
    const stageIds = Object.keys(template.findResources('AWS::ApiGateway::Stage'));
    expect(stageIds).toHaveLength(1);

    const assoc = Object.values(
      template.findResources('AWS::WAFv2::WebACLAssociation'),
    )[0] as any;
    expect(assoc.DependsOn).toBeDefined();
    expect(assoc.DependsOn).toEqual(expect.arrayContaining([stageIds[0]]));
  });
});
