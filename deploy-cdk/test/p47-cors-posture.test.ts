// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as fs from 'fs';
import * as path from 'path';
import { FoundationStack } from '../lib/foundation-stack';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import { ComputeStack } from '../lib/compute-stack';
import { HostingStack } from '../lib/hosting-stack';

/**
 * The two facts that make `Access-Control-Allow-Origin: "*"` correct in
 * `shared/response_utils.py`, and the WAF that backs them up — p47 R13.1, R13.4,
 * R13.5; E4's evaluation §2 and §2.3 items 2 and 3.
 *
 * WHY THIS EXISTS
 * ---------------
 * p47 R13 asked for the wildcard origin to be narrowed. E4 measured the threat
 * model and the answer was **the wildcard is already correct**, for two reasons
 * that live nowhere near the line that emits it:
 *
 *   1. The deployed path is SAME-ORIGIN. `hosting-stack.ts` deploys `config.json`
 *      with `apiBaseUrl: '/api'` and attaches the `/api/*` behaviour to the same
 *      distribution whose default behaviour serves the SPA from S3. A same-origin
 *      fetch sends no `Origin` header and never reads
 *      `Access-Control-Allow-Origin`, so the wildcard is emitted and discarded on
 *      every production request.
 *   2. The credential is not ambient. The Cognito token is attached explicitly by
 *      `api-client.ts` and held in `sessionStorage`, never a cookie;
 *      `Access-Control-Allow-Credentials` is absent repo-wide and is
 *      spec-incompatible with `*` anyway. A hostile page can make the request the
 *      wildcard permits, but arrives unauthenticated and gets 401.
 *
 * Fact 1 was **unasserted anywhere** before this file, which is what made it
 * dangerous rather than merely undocumented: change `apiBaseUrl` to an absolute
 * cross-origin URL and the wildcard silently becomes load-bearing, with nobody
 * re-deriving the argument above. E4 called that assertion the highest-value
 * artifact in the whole of R13, ahead of narrowing the header. It is the first
 * describe block below.
 *
 * The second describe block closes the other half of E4's §2.3: the
 * `X-Origin-Verify` control that makes direct `execute-api` access return 403 was
 * only pinned on the SENDER side (`hosting-auth.test.ts` asserts CloudFront sets
 * the header). The WAF that CHECKS it was unasserted, so it could have been
 * deleted — or had `defaultAction` flipped to `Allow` — with every test still
 * green. Worth doing under any R13 arm.
 *
 * WHY THE SYNTHESIZED TEMPLATE AND THE STAGED ASSET, NOT THE SOURCE
 * -----------------------------------------------------------------
 * `LGR-1166391-1` settled this for CDK-shape guards: assert on what
 * CloudFormation and S3 are actually handed, because a source-text scan passes on
 * input that still fails at deploy. It also recorded that the Python `tests/cdk/`
 * suite cannot render a template at all, which is why this is vitest.
 *
 * `config.json` is a deliberate exception to "assert the template": `Source.jsonData`
 * writes the JSON into an ASSET, not into the template, so the template carries
 * only the asset's object key. This file follows that key into the staged asset
 * directory and reads the file that will be uploaded to S3 — one step closer to
 * the deployed artifact than the template is.
 *
 * WHAT THIS FILE DOES NOT CLAIM
 * -----------------------------
 * Not that any live request behaves differently. Both facts above say the
 * production browser never consults this header; the edge policy asserted below
 * changes the browser-visible posture and the auditability of it, not the
 * behaviour of any request that exists today. Whether CloudFront omits the header
 * entirely or substitutes a listed origin when a request's `Origin` matches
 * nothing in the list was not deploy-verified — only that `originOverride: true`
 * means the origin's own `*` is not what reaches the viewer, which is what the
 * AWS response-headers-policy documentation states.
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';

/**
 * The load-bearing value. `/api` is RELATIVE, which is what makes the dashboard
 * and the API the same origin. An absolute URL on another host breaks fact 1.
 */
const EXPECTED_API_BASE_URL = '/api';

/** The only origin that is ever legitimately cross-origin — see R13.4 in hosting-stack.ts. */
const EXPECTED_CORS_ORIGINS = ['http://localhost:5173'];

/** Kept identical to `shared/response_utils.py`'s `default_headers`. */
const EXPECTED_CORS_HEADERS = [
  'Content-Type',
  'X-Amz-Date',
  'Authorization',
  'X-Api-Key',
  'X-Amz-Security-Token',
];
const EXPECTED_CORS_METHODS = ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'];

const ORIGIN_VERIFY_HEADER = 'x-origin-verify';
const API_STAGE_NAME = 'v1';

interface SynthedHosting {
  template: Record<string, any>;
  /** Contents of every `config.json` reachable from the BucketDeployment's asset keys. */
  configs: Record<string, any>[];
}

function synthesizeHosting(allowLocalhost = false): SynthedHosting {
  // Bundling suppressed for the reason `p47-strands-layer-attachment.test.ts` and
  // `hosting-auth.test.ts` suppress it: the dashboard asset would otherwise run a
  // real `npm ci && npm run build`, and its content cannot affect either the
  // config.json source or the CloudFront policy shape.
  //
  // `adi:allowLocalhost` (R-12) decides whether the /api/* policy narrows CORS to
  // the dev host or strips the header; both arms are asserted below.
  const context: Record<string, any> = { 'aws:cdk:bundling-stacks': [] };
  if (allowLocalhost) context['adi:allowLocalhost'] = 'true';
  const app = new cdk.App({ context });
  const stack = new HostingStack(app, `AgenticDataIngestion-Hosting-${TEST_ENV}`, {
    env: { account: TEST_ACCOUNT, region: TEST_REGION },
    environment: TEST_ENV,
    restApiHost: 'abc123.execute-api.us-west-2.amazonaws.com',
    originVerifySecretValue: 'test-secret-value-for-cdk-assertions',
    cloudFrontWebAclArn: 'arn:aws:wafv2:us-east-1:123456789012:global/webacl/test/abc',
    userPoolId: 'us-west-2_TestPool',
    userPoolArn: `arn:aws:cognito-idp:${TEST_REGION}:${TEST_ACCOUNT}:userpool/us-west-2_TestPool`,
    hostedUiDomain:
      'https://agentic-data-ingestion-111111111111-dev.auth.us-west-2.amazoncognito.com',
    wsUrl: 'wss://ws123.execute-api.us-west-2.amazonaws.com/prod',
  });

  const assembly = app.synth();
  const template = assembly.getStackArtifact(stack.artifactId).template as Record<string, any>;

  // Follow the deployment's own asset keys rather than globbing every asset.* dir,
  // so this reads the object that WILL be uploaded, not some other staged file
  // that happens to be named config.json.
  const configs: Record<string, any>[] = [];
  for (const resource of Object.values(template.Resources ?? {}) as any[]) {
    if (resource.Type !== 'Custom::CDKBucketDeployment') continue;
    for (const key of (resource.Properties?.SourceObjectKeys ?? []) as string[]) {
      const configPath = path.join(
        assembly.directory,
        `asset.${key.replace(/\.zip$/, '')}`,
        'config.json',
      );
      if (!fs.existsSync(configPath)) continue;
      // Values that were CDK tokens are written as bare `<<marker:0x…:N>>`
      // placeholders and substituted by the deployment custom resource, which makes
      // the raw file invalid JSON. Quote them so the literal keys stay readable.
      // `apiBaseUrl` is asserted below to be a literal, so it is never a marker —
      // and if it became one, that would itself be the regression: a deploy-time
      // value cannot be checked here at all.
      const raw = fs.readFileSync(configPath, 'utf-8').replace(/<<marker:[^>]*>>/g, '"<<token>>"');
      configs.push(JSON.parse(raw));
    }
  }
  return { template, configs };
}

function synthesizeCompute(): Record<string, any> {
  const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
  const env = { account: TEST_ACCOUNT, region: TEST_REGION };

  const foundation = new FoundationStack(app, `AgenticDataIngestion-Foundation-${TEST_ENV}`, {
    env,
    projectName: 'agentic-data-ingestion',
    environment: TEST_ENV,
  });
  const data = new DataStack(app, `AgenticDataIngestion-Data-${TEST_ENV}`, {
    env,
    environment: TEST_ENV,
  });
  const auth = new AuthStack(app, `AgenticDataIngestion-Auth-${TEST_ENV}`, {
    env,
    environment: TEST_ENV,
  });
  const compute = new ComputeStack(app, `AgenticDataIngestion-Compute-${TEST_ENV}`, {
    env,
    environment: TEST_ENV,
    projectName: 'agentic-data-ingestion',
    foundation,
    data,
    // The whole edge-hardening block is inside `if (props.userPoolArn)`, so the WAF
    // only exists when this is passed. `bin/agentic_data_ingestion.ts` passes it
    // unconditionally, so this matches every documented deploy path.
    userPoolArn: auth.userPool.userPoolArn,
  });

  return Template.fromStack(compute).toJSON();
}

/** Flatten an `Fn::Join` / `Ref` / `Fn::GetAtt` tree into something greppable. */
function flatten(value: any): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(flatten).join('');
  if (value && typeof value === 'object') {
    if (value['Fn::Join']) {
      const [sep, parts] = value['Fn::Join'];
      return (parts as any[]).map(flatten).join(sep);
    }
    if (value.Ref) return `\${${value.Ref}}`;
    if (value['Fn::GetAtt']) return `\${${(value['Fn::GetAtt'] as string[]).join('.')}}`;
    return Object.values(value).map(flatten).join('');
  }
  return '';
}

describe('HostingStack — the same-origin premise behind the wildcard CORS origin (p47 R13)', () => {
  let synthed: SynthedHosting;

  beforeAll(() => {
    synthed = synthesizeHosting();
  }, 300_000);

  it('deploys a config.json, so the assertion below resolves something', () => {
    // Without this, a change to how runtime config is delivered makes the
    // apiBaseUrl assertion compare against nothing and pass vacuously.
    expect(
      synthed.configs.length,
      'no config.json found in any asset referenced by the BucketDeployment. Runtime ' +
        'config delivery has changed shape; the apiBaseUrl assertion below is now ' +
        'vacuous and must be re-pointed at wherever apiBaseUrl now lives.',
    ).toBeGreaterThan(0);
  });

  it(`deploys apiBaseUrl === '${EXPECTED_API_BASE_URL}', keeping the dashboard and the API same-origin`, () => {
    // THE assertion this file exists for.
    for (const config of synthed.configs) {
      expect(
        config.apiBaseUrl,
        `config.json has apiBaseUrl='${config.apiBaseUrl}', not '${EXPECTED_API_BASE_URL}'.\n` +
          '\n' +
          'THIS IS A CORS DECISION, not a URL preference. `shared/response_utils.py` emits ' +
          '`Access-Control-Allow-Origin: "*"` on all 332 of its call sites, and p47 R13 kept it ' +
          'because a RELATIVE apiBaseUrl makes the dashboard and the API the same origin — same ' +
          'scheme, host and port — so the browser sends no Origin header, runs no preflight, and ' +
          'never reads that header at all.\n' +
          '\n' +
          'An absolute URL on a different host than the SPA destroys that premise and makes the ' +
          'wildcard load-bearing: every authenticated API response becomes readable by any origin ' +
          'that can get a token in front of a browser. If you genuinely need a cross-origin API ' +
          'base, the wildcard in response_utils.py must be narrowed IN THE SAME CHANGE — see the ' +
          'comment at its definition, the p47 E6 decision record, and R13.3.\n' +
          '\n' +
          'An absolute URL on the SAME host is still same-origin and is not what this guards ' +
          'against; if that is what you are doing, widen this assertion deliberately and say so.',
      ).toBe(EXPECTED_API_BASE_URL);
    }
  });

  it('serves the SPA and /api/* from one distribution, which is the other half of same-origin', () => {
    // apiBaseUrl='/api' only means same-origin because /api/* is a behaviour on the
    // SAME distribution that serves index.html. Split them across two
    // distributions and the relative path would resolve to a different host.
    const distributions = Object.values(synthed.template.Resources ?? {}).filter(
      (r: any) => r.Type === 'AWS::CloudFront::Distribution',
    ) as any[];
    expect(
      distributions.length,
      'expected exactly one CloudFront distribution: the relative apiBaseUrl is only ' +
        'same-origin because the SPA and /api/* share a distribution.',
    ).toBe(1);

    const config = distributions[0].Properties.DistributionConfig;
    expect(config.DefaultCacheBehavior, 'the SPA behaviour must be on this distribution').toBeDefined();
    expect(
      (config.CacheBehaviors ?? []).map((b: any) => b.PathPattern),
      'the /api/* behaviour must be on the SAME distribution as the SPA, or a relative ' +
        'apiBaseUrl stops being same-origin',
    ).toContain('/api/*');
  });
});

/** The /api/* response-headers policy (the other one is the SPA's, SpaSecurityHeaders). */
function apiPolicy(template: Record<string, any>): { policy: Record<string, any>; logicalId: string } {
  const hits = (Object.entries(template.Resources ?? {}) as [string, any][]).filter(
    ([logicalId, r]) => r.Type === 'AWS::CloudFront::ResponseHeadersPolicy' && logicalId.startsWith('ApiCorsHeaders'),
  );
  expect(hits, 'expected exactly one ApiCorsHeaders policy').toHaveLength(1);
  return { policy: hits[0][1].Properties.ResponseHeadersPolicyConfig, logicalId: hits[0][0] };
}

describe('HostingStack — CORS narrowed at the edge on /api/*, with -c adi:allowLocalhost=true (p47 R13.3 arm 4, R13.4, R13.5)', () => {
  let synthed: SynthedHosting;
  let policy: Record<string, any> | undefined;
  let policyLogicalId: string | undefined;

  beforeAll(() => {
    synthed = synthesizeHosting(true);
    ({ policy, logicalId: policyLogicalId } = apiPolicy(synthed.template));
  }, 300_000);

  it('renders a ResponseHeadersPolicy with a CORS config', () => {
    expect(
      policy?.CorsConfig,
      'no AWS::CloudFront::ResponseHeadersPolicy with a CorsConfig in the Hosting template. ' +
        'Deleting it returns the browser-visible Access-Control-Allow-Origin on /api/* to the ' +
        "Lambda's `*` — see hosting-stack.ts and p47 R13.3.",
    ).toBeDefined();
  });

  it('is attached to the /api/* behaviour, which is the only reason it does anything', () => {
    const distribution = Object.values(synthed.template.Resources ?? {}).find(
      (r: any) => r.Type === 'AWS::CloudFront::Distribution',
    ) as any;
    const apiBehavior = (distribution.Properties.DistributionConfig.CacheBehaviors ?? []).find(
      (b: any) => b.PathPattern === '/api/*',
    );
    expect(
      apiBehavior?.ResponseHeadersPolicyId,
      'the /api/* behaviour carries no ResponseHeadersPolicyId. An unattached policy is inert: ' +
        "the Lambda's `*` reaches the viewer unchanged.",
    ).toEqual({ Ref: policyLogicalId });
  });

  it('narrows Access-Control-Allow-Origin to the documented cross-origin dev host (R13.4)', () => {
    expect(
      policy!.CorsConfig.AccessControlAllowOrigins.Items,
      'the allowed-origin list changed.\n' +
        `Expected exactly ${JSON.stringify(EXPECTED_CORS_ORIGINS)} — the ONE origin that is ever ` +
        'legitimately cross-origin here, per docs/LOCAL_DEVELOPMENT.md §4 (the ' +
        'VITE_API_BASE_URL mode, which skips the Vite proxy and calls CloudFront directly). ' +
        'Removing it breaks that documented mode as a confusing CORS failure, not a deploy error.\n' +
        '\n' +
        "DO NOT ADD the distribution's own domain name here. It is not needed — the SPA and the " +
        'API share scheme/host/port, so those requests carry no Origin header — and referencing ' +
        '`distributionDomainName` emits a CloudFormation CIRCULAR DEPENDENCY that `cdk synth` ' +
        'accepts silently and the deploy then rejects: the behaviour Refs this policy while this ' +
        "policy GetAtts the distribution's DomainName.",
    ).toEqual(EXPECTED_CORS_ORIGINS);
  });

  it('does not contain a wildcard origin — the point of the exercise', () => {
    // Stated separately so the failure reads as a posture regression rather than a
    // list mismatch. `'*'` here would restore exactly what R13 set out to narrow.
    expect(
      policy!.CorsConfig.AccessControlAllowOrigins.Items,
      "'*' in the edge allow-list makes this policy a no-op relative to the Lambda's own header, " +
        'which is the state p47 R13 was raised to change.',
    ).not.toContain('*');
  });

  it('overrides the origin header rather than passing it through', () => {
    // The entire mechanism. With OriginOverride false, CloudFront forwards the
    // header it received from the origin — the Lambda's `*` — and the narrowed list
    // above is never used.
    expect(
      policy!.CorsConfig.OriginOverride,
      'OriginOverride must be true. When it is false CloudFront keeps the CORS header it ' +
        "received from the origin, so `shared/response_utils.py`'s `*` reaches the browser and " +
        'the allow-list above has no effect at all.',
    ).toBe(true);
  });

  it('never sets Access-Control-Allow-Credentials', () => {
    // Spec-incompatible with a wildcard, absent from the whole repository, and no
    // request here carries a cookie: the Cognito token travels in an explicit
    // Authorization header. Turning this on would be a new auth posture, not a
    // CORS tweak.
    expect(
      policy!.CorsConfig.AccessControlAllowCredentials,
      'AccessControlAllowCredentials must stay false. It is spec-incompatible with a wildcard ' +
        'origin (which the Lambda still emits), the header appears nowhere in this repository, ' +
        'and the credential here is a bearer token in an explicit header, never a cookie.',
    ).toBe(false);
  });

  it('keeps the edge header/method lists identical to what the Lambda emits', () => {
    // Divergence here is the failure mode this policy introduces: the edge would
    // silently answer preflights with a different contract than the origin does.
    expect(
      policy!.CorsConfig.AccessControlAllowHeaders.Items,
      "must match `default_headers['Access-Control-Allow-Headers']` in shared/response_utils.py",
    ).toEqual(EXPECTED_CORS_HEADERS);
    expect(
      policy!.CorsConfig.AccessControlAllowMethods.Items,
      "must match `default_headers['Access-Control-Allow-Methods']` in shared/response_utils.py",
    ).toEqual(EXPECTED_CORS_METHODS);
  });

  it('resolves the origin list to concrete strings, so there is no missing-value case (R13.5)', () => {
    // R13.5 asks that a missing or empty origin fail closed to current behaviour
    // rather than emit a malformed header. Under this arm that is STRUCTURAL rather
    // than guarded: CDK writes literal strings into the template, so there is no
    // variable to be absent and no `""` or `"None"` to leak. This asserts that
    // property directly — an unresolved token or empty entry here would mean the
    // value had become runtime-dependent again.
    for (const origin of policy!.CorsConfig.AccessControlAllowOrigins.Items) {
      expect(
        typeof origin,
        'every allowed origin must be a literal string in the template. An intrinsic (Ref / ' +
          'Fn::GetAtt / Fn::Join) means the value is resolved at deploy or runtime, which ' +
          'reintroduces the missing-or-empty case R13.5 exists to prevent — and, if it points ' +
          'at this distribution, a circular dependency.',
      ).toBe('string');
      expect(origin.length, 'an empty origin string is a malformed header (R13.5)').toBeGreaterThan(0);
    }
  });
});

describe('HostingStack — without adi:allowLocalhost, /api/* strips Access-Control-Allow-Origin (R-12)', () => {
  // The default since R-12. There is no cross-origin caller to allow, and
  // CloudFront requires at least one origin in a CORS config, so the policy
  // carries no CorsConfig and removes the header instead. That strips the
  // Lambda's `*` and API Gateway's preflight `*` alike.
  let synthed: SynthedHosting;
  let policy: Record<string, any>;
  let policyLogicalId: string;

  beforeAll(() => {
    synthed = synthesizeHosting(false);
    ({ policy, logicalId: policyLogicalId } = apiPolicy(synthed.template));
  }, 300_000);

  it('carries no CORS config, so no origin is ever allowed', () => {
    expect(policy.CorsConfig).toBeUndefined();
  });

  it("removes the origin's Access-Control-Allow-Origin", () => {
    const removed = (policy.RemoveHeadersConfig?.Items ?? []).map((i: any) => i.Header);
    expect(removed).toEqual(['Access-Control-Allow-Origin']);
  });

  it('is still attached to the /api/* behaviour', () => {
    const distribution = Object.values(synthed.template.Resources ?? {}).find(
      (r: any) => r.Type === 'AWS::CloudFront::Distribution',
    ) as any;
    const apiBehavior = (distribution.Properties.DistributionConfig.CacheBehaviors ?? []).find(
      (b: any) => b.PathPattern === '/api/*',
    );
    expect(apiBehavior?.ResponseHeadersPolicyId).toEqual({ Ref: policyLogicalId });
  });

  it('names localhost nowhere in the template — CORS and Cognito move together (LGR-f34fe5c-3)', () => {
    expect(JSON.stringify(synthed.template)).not.toContain('localhost');
  });
});

describe('ComputeStack — the X-Origin-Verify receiver half (p47 E4 §2.3 item 3, finding 39)', () => {
  let template: Record<string, any>;
  let webAcl: Record<string, any> | undefined;
  let webAclLogicalId: string | undefined;

  beforeAll(() => {
    template = synthesizeCompute();
    for (const [logicalId, resource] of Object.entries(template.Resources ?? {}) as any[]) {
      if (resource.Type === 'AWS::WAFv2::WebACL' && resource.Properties?.Scope === 'REGIONAL') {
        webAcl = resource.Properties;
        webAclLogicalId = logicalId;
      }
    }
  }, 300_000);

  it('renders a REGIONAL web ACL on the REST API', () => {
    expect(
      webAcl,
      'no REGIONAL AWS::WAFv2::WebACL in the Compute template. This is the control that makes ' +
        'direct execute-api access return 403 (docs/DEPLOY_HOSTING.md, docs/TROUBLESHOOTING.md) ' +
        'and the defence-in-depth E4 cites alongside the Cognito authorizer. Before this file it ' +
        'could be deleted with every test still green.',
    ).toBeDefined();
    expect(webAclLogicalId, 'expected the ApiRegionalWaf construct id').toContain('ApiRegionalWaf');
  });

  it('blocks by default, so anything not explicitly allowed is rejected', () => {
    // The load-bearing half. Flipping this to Allow leaves the ACL in place, keeps
    // every other assertion passing, and silently opens direct execute-api access.
    expect(
      webAcl!.DefaultAction,
      'the REGIONAL WAF must default to Block. With Allow as the default the single rule below ' +
        'becomes decorative and the API is reachable directly, bypassing CloudFront and the ' +
        'X-Origin-Verify check entirely.',
    ).toEqual({ Block: {} });
  });

  it('allows exactly one thing: an exact match on the x-origin-verify header', () => {
    expect(webAcl!.Rules, 'expected a single Allow rule').toHaveLength(1);
    const rule = webAcl!.Rules[0];
    expect(rule.Action, 'the one rule must Allow').toEqual({ Allow: {} });

    const byteMatch = rule.Statement?.ByteMatchStatement;
    expect(
      byteMatch,
      'the rule must be a ByteMatchStatement on the origin-verify header; any other statement ' +
        'shape means the CloudFront-only guarantee is no longer what is being checked',
    ).toBeDefined();
    // `singleHeader` is typed `any` on CfnWebACL, so CDK passes its inner key
    // through verbatim. The AWS::WAFv2::WebACL schema requires `Name`; a
    // lowercase `name` deployed only because CloudFormation's handler normalised
    // it, and CDK's synth-time CloudFormation Validate flags it (F3002/F3003).
    // So the exact object is pinned: the PascalCase key, and no lowercase one.
    expect(
      byteMatch.FieldToMatch?.SingleHeader,
      `must match the ${ORIGIN_VERIFY_HEADER} header under the schema's \`Name\` key`,
    ).toEqual({ Name: ORIGIN_VERIFY_HEADER });
    expect(
      byteMatch.PositionalConstraint,
      'EXACTLY, not CONTAINS or STARTS_WITH: a prefix match on a shared secret would accept a ' +
        'truncated value',
    ).toBe('EXACTLY');
    expect(
      byteMatch.TextTransformations,
      'no text transformation: the value is compared byte-for-byte against the secret',
    ).toEqual([{ Priority: 0, Type: 'NONE' }]);
  });

  it('associates the ACL with the API STAGE, not the API — and depends on the stage', () => {
    // The part most worth pinning, and the only one with a live failure behind it.
    // compute-stack.ts carries an in-file comment recording it: building resourceArn
    // from the restApiId token makes CloudFormation infer a dependency on the
    // RestApi but NOT on the 'v1' stage, so a fresh deploy can create the
    // association before the stage exists and fail at the AWS API with WAFv2
    // NotFound. `cdk synth` does not catch that, which is exactly why it needs a
    // test rather than a comment. Asserted here against the REAL Compute template
    // — `hosting-auth.test.ts` reproduces the pattern in a mock stack, which cannot
    // notice if compute-stack.ts stops using it.
    const associations = Object.values(template.Resources ?? {}).filter(
      (r: any) => r.Type === 'AWS::WAFv2::WebACLAssociation',
    ) as any[];
    expect(associations, 'expected one WebACLAssociation for the REGIONAL WAF').toHaveLength(1);

    const resourceArn = flatten(associations[0].Properties.ResourceArn);
    expect(
      resourceArn,
      `the association targets '${resourceArn}', which is not a stage ARN. Associating the ` +
        'RestApi instead of its stage leaves the deployed stage unprotected, and the stage ARN ' +
        'is also what makes the dependency below possible.',
    ).toContain('/stages/');

    // The stage segment must be a Ref to an actual AWS::ApiGateway::Stage, not a
    // hand-built name. `${stage}` rather than the literal `v1` is what the ARN token
    // renders as, and resolving it to a resource is what proves this is the stage.
    const stageRef = /\/stages\/\$\{([^}]+)\}/.exec(resourceArn)?.[1];
    expect(
      stageRef && template.Resources?.[stageRef]?.Type,
      `the '/stages/' segment resolves to '${stageRef}', which is not an AWS::ApiGateway::Stage. ` +
        `A hand-built ARN (e.g. a literal '${API_STAGE_NAME}') would associate a stage name that ` +
        'CloudFormation has no dependency on — the WAFv2 NotFound race in full.',
    ).toBe('AWS::ApiGateway::Stage');

    expect(
      associations[0].DependsOn,
      `the association must carry an explicit DependsOn for '${stageRef}'. Without it a fresh ` +
        "deploy races the stage's creation and fails at the AWS API with WAFv2 NotFound — " +
        'CloudFormation infers a dependency on the RestApi from the restApiId token but NOT on ' +
        'the stage, and `cdk synth` cannot see the difference. Do not simplify the ' +
        'apiWafAssociation.node.addDependency(...) call in compute-stack.ts away.',
    ).toEqual(expect.arrayContaining([stageRef]));
  });
});
