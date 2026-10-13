// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { HostingStack } from '../lib/hosting-stack';
import { SPA_ROUTER_FUNCTION_CODE } from '../lib/spa-router-function';

/**
 * The SPA fallback is a per-behaviour CloudFront Function, not a
 * distribution-wide error mapping — and `/api*` error statuses survive to the
 * client.
 *
 * ## The defect this pins shut
 *
 * `hosting-stack.ts` carried `errorResponses` mapping **403 and 404 to HTTP 200
 * with the body of `/index.html`**. CloudFront custom error responses live on
 * `DistributionConfig` and cannot be scoped to a behaviour, so that mapping also
 * rewrote everything the `/api/*` behaviour produced. The full chain, each step
 * read in the tree rather than inferred:
 *
 * 1. `handlers/pipelines_artifacts.py:89` answers `create_response(404, …)` when a
 *    code version has no test-results rows yet.
 * 2. The distribution rewrote that to 200 + index.html — HTML, not JSON.
 * 3. `services/api-client.ts:190-194` sees `res.ok` true and `.json()` throwing,
 *    so it raises `ApiError(200, 'Response body could not be read as JSON (HTTP
 *    200)')`, which reaches the operator as a toast.
 * 4. `hooks/use-code.ts:56` converts only a **404** to the friendly pending
 *    state, so it rethrew and `TestResultsViewer` rendered *"Failed to load test
 *    results."* with a Retry that was deterministic and therefore useless.
 *
 * 403 took the same path, so a WAF block or a Cognito authorizer rejection on
 * `/api/*` also arrived as a non-JSON 200.
 *
 * ## Why vitest and not `tests/cdk/`
 *
 * `LGR-1166391-1` (**template / ratified**) settles this for CDK-shape guards:
 * assert on the synthesized template, because "a source scan can pass on input
 * that still fails at deploy", and the Python `tests/cdk/` layer "cannot produce
 * a template at all" — `aws-cdk-lib` is absent from `pyproject.toml` and every
 * file there asserts on `.ts` source as text. What matters here is exactly a
 * template-level fact: whether `FunctionAssociations` is on
 * `DefaultCacheBehavior` and absent from the `/api/*` entry of `CacheBehaviors`,
 * and whether `CustomErrorResponses` is present at all. A source-text scan could
 * not distinguish a function that is declared from one that is attached, which is
 * the whole distinction. So: vitest, over `app.synth()`.
 *
 * ## Why the function body is compiled rather than string-matched
 *
 * A CloudFront Function is JavaScript, and `SPA_ROUTER_FUNCTION_CODE` is the
 * exact string handed to CloudFront. The second describe block compiles **that
 * same string** with `new Function` and exercises the resulting `handler` over a
 * table of real paths. Asserting on substrings of the source would test the
 * spelling of the logic instead of the logic, and could not catch an off-by-one
 * in the segment arithmetic.
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';

// ─────────────────────────────────────────────────────────────────────────────
// Documented CloudFront bounds that `cdk synth` does not enforce
//
// This construct hands CloudFront three strings — a comment, a name and the
// function body — and each has a documented maximum that nothing between the
// TypeScript and the API call checks. `cdk synth` renders all three happily;
// the CloudFront API is the first thing that measures them, and it measures
// them during CREATE, which means a rollback rather than a test failure.
//
// That gap is not hypothetical here, it has now cost two deploys. The sibling
// budget assertion (`p55-schema-extract-layer-budget.test.ts`) states the same
// limitation in its header for Lambda layer sizes, and `p47-cors-posture.test.ts`
// records two more cases where synth accepted what the service rejected (a
// circular dependency, and a WAF association racing a stage). The bounds below
// are the cheap half of that class: the value is a literal in the template, so
// asserting it costs one property read.
//
// Each constant is the DOCUMENTED figure with its source, and each assertion
// tests the BOUND. A rewording, a rename or an added line of function logic must
// stay free; only an over-length one may fail.
// ─────────────────────────────────────────────────────────────────────────────

/**
 * `FunctionConfig.Comment` — maximum 128 characters, inclusive.
 *
 * Documented in two places, both consulted:
 *  - https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-cloudfront-function-functionconfig.html
 *    — "The comment cannot be longer than 128 characters."
 *  - https://docs.aws.amazon.com/cloudfront/latest/APIReference/API_FunctionConfig.html
 *    — the same sentence, on the API this ultimately calls.
 *
 * THE INCIDENT THIS RECORDS. On **2026-08-31** the
 * `AgenticDataIngestion-Hosting-dev` deploy failed with:
 *
 *     CREATE_FAILED | AWS::CloudFront::Function | SpaRouterFunction95E96187
 *     Resource handler returned message: "Invalid request provided:
 *     AWS::CloudFront::Function: The parameter Comment is too big.
 *     (Service: CloudFront, Status Code: 400)"
 *
 * The comment was **136 characters** — 8 over. The stack rolled back cleanly
 * (`UPDATE_ROLLBACK_COMPLETE`, distribution untouched), so the cost was a wasted
 * deploy cycle rather than damage, but `npx cdk synth` had accepted it and every
 * suite in the repo was green. The assertion below is the thing that would have
 * caught it, in about a second.
 */
const CLOUDFRONT_FUNCTION_COMMENT_MAX_CHARS = 128;

/**
 * `AWS::CloudFront::Function.Name` — maximum 64 characters, pattern
 * `[a-zA-Z0-9-_]{1,64}`.
 *
 * https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-cloudfront-function.html
 * ("*Maximum*: `64`", "*Pattern*: `[a-zA-Z0-9-_]{1,64}`")
 *
 * The obvious second candidate, because the name is deliberately long: it is
 * scoped by account AND region (publication-cleanup R11 — CloudFront Function
 * names are unique per account, so a deterministic unscoped name collides across
 * deployments), which spends 22 characters before the environment suffix.
 *
 * Rendered here as `adi-spa-router-111111111111-us-west-2-dev` — **41 of 64**.
 * The headroom survives the realistic substitutions: the longest current AWS
 * region names are 14 characters (`ap-southeast-4`, 5 more than `us-west-2`) and
 * an environment of `staging` is 4 more than `dev`, which together reach 50. So
 * this is not close today, and the assertion exists for the same reason the
 * comment one does — the next edit to the template is not checked by hand, and
 * `Name` additionally requires **Replacement** on update, so an over-length one
 * fails on the way to replacing a live function.
 *
 * The charset is asserted alongside the length because it is the same class of
 * defect from the other direction: an environment name containing a dot or a
 * slash renders a Name that synth accepts and CloudFront rejects.
 */
const CLOUDFRONT_FUNCTION_NAME_MAX_CHARS = 64;
const CLOUDFRONT_FUNCTION_NAME_PATTERN = /^[a-zA-Z0-9\-_]{1,64}$/;

/**
 * The function body — maximum function size **10 KB**, not adjustable.
 *
 * https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cloudfront-limits.html
 * ("Quotas on CloudFront Functions" → "Maximum function size … 10 KB … This quota
 * isn't adjustable.")
 *
 * TWO READINGS EXIST AND THE STRICTER ONE IS USED DELIBERATELY. "10 KB" is
 * ambiguous between 10,000 and 10,240 bytes, and unlike the Lambda unzipped limit
 * in `p55-schema-extract-layer-budget.test.ts` there is no deployed measurement
 * in this tree that settles which is live. So this takes 10 x 10^3, the smaller
 * of the two: passing under it passes under either reading, and re-measuring can
 * only ever move the bound in the safe direction. Measured in **UTF-8 bytes**
 * (`Buffer.byteLength`) rather than characters for the same reason — bytes are
 * >= characters, so the byte reading cannot under-report.
 *
 * Included even though `SPA_ROUTER_FUNCTION_CODE` currently renders **942 bytes /
 * 9.4%**, because it is the one of the three strings this construct sets that is
 * *expected* to grow: the module docstring's own `revisit_when` — and
 * `LGR-4d242bf-1`'s — is to narrow the extension heuristic to an allowlist, which
 * means more code. A budget with 9 KB of slack is still a budget.
 */
const CLOUDFRONT_FUNCTION_CODE_MAX_BYTES = 10_000;

// ─────────────────────────────────────────────────────────────────────────────
// Template assertions
// ─────────────────────────────────────────────────────────────────────────────

interface Synthed {
  template: Record<string, any>;
  distributionConfig: Record<string, any>;
}

function synthesizeHosting(): Synthed {
  // Bundling suppressed for the reason `hosting-auth.test.ts` and
  // `p47-cors-posture.test.ts` suppress it: the dashboard asset would otherwise
  // run a real `npm ci && npm run build`, and its content cannot affect the
  // distribution shape being asserted here.
  const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
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

  const distributions = Object.values(template.Resources ?? {}).filter(
    (r: any) => r.Type === 'AWS::CloudFront::Distribution',
  ) as any[];
  expect(distributions, 'expected exactly one CloudFront distribution').toHaveLength(1);

  return { template, distributionConfig: distributions[0].Properties.DistributionConfig };
}

describe('HostingStack — SPA fallback is per-behaviour, /api/* errors survive', () => {
  let synthed: Synthed;
  let functionLogicalId: string | undefined;
  let functionResource: any;

  beforeAll(() => {
    synthed = synthesizeHosting();
    for (const [logicalId, resource] of Object.entries(synthed.template.Resources ?? {}) as any[]) {
      if (resource.Type === 'AWS::CloudFront::Function') {
        functionLogicalId = logicalId;
        // Captured as well as the id: the service-bound assertions at the foot of
        // this block read Properties off the RENDERED resource, which is the
        // whole point — the TypeScript is not what CloudFront measures.
        functionResource = resource;
      }
    }
  }, 300_000);

  it('renders exactly one CloudFront Function', () => {
    // Without this the association assertions below could pass vacuously against
    // an undefined logical id.
    const functions = Object.values(synthed.template.Resources ?? {}).filter(
      (r: any) => r.Type === 'AWS::CloudFront::Function',
    );
    expect(
      functions,
      'expected one AWS::CloudFront::Function — the SPA deep-link rewrite. Deleting it ' +
        'breaks every extensionless route (/, /pipelines, /approvals, /auth/callback) because ' +
        'there is no longer an errorResponses fallback behind it.',
    ).toHaveLength(1);
    expect(functionLogicalId).toBeDefined();
  });

  // ── The whole point: NO distribution-wide error mapping ───────────────────
  it('declares NO CustomErrorResponses at all', () => {
    expect(
      synthed.distributionConfig.CustomErrorResponses,
      'CustomErrorResponses is back on the Hosting distribution.\n' +
        '\n' +
        'DO NOT REINTRODUCE IT. CloudFront custom error responses are a property of\n' +
        'DistributionConfig and CANNOT be scoped to a cache behaviour, so any entry here also\n' +
        'rewrites the responses of the /api/* behaviour. That is the deployed defect this file\n' +
        'exists to keep shut:\n' +
        '\n' +
        '  - GET /api/tests/{id} answers a real 404 when tests have not run yet\n' +
        '    (handlers/pipelines_artifacts.py:89).\n' +
        '  - A 404 -> 200 + /index.html entry turns that into HTML under a success status.\n' +
        '  - api-client._unwrap (services/api-client.ts:190-194) then raises\n' +
        '    ApiError(200, "Response body could not be read as JSON (HTTP 200)"), which the\n' +
        '    operator sees as a toast.\n' +
        '  - hooks/use-code.ts:56 only converts a 404 to the pending state, so the panel\n' +
        '    reports "Failed to load test results." for a pipeline that is simply not tested yet.\n' +
        '  - The 403 entry did the same to every WAF block and Cognito authorizer rejection.\n' +
        '\n' +
        'The SPA fallback it used to provide is now the viewer-request CloudFront Function in\n' +
        'lib/spa-router-function.ts, attached to the DEFAULT behaviour only. If a route is\n' +
        'reaching S3 and 403-ing, extend that function\'s heuristic and its path table in this\n' +
        'file — do not add an error mapping back.',
    ).toBeUndefined();
  });

  it('attaches the function to the DEFAULT behaviour on viewer-request', () => {
    const associations = synthed.distributionConfig.DefaultCacheBehavior?.FunctionAssociations;
    expect(
      associations,
      'the default (S3) behaviour carries no FunctionAssociations. An unattached function is ' +
        'inert, and with errorResponses gone there is nothing else serving SPA deep links — ' +
        'every extensionless route would 403 from S3.',
    ).toBeDefined();
    expect(associations).toHaveLength(1);
    expect(associations[0].EventType).toBe('viewer-request');
    expect(
      associations[0].FunctionARN,
      'the association must reference the SPA router function resource, not a literal ARN',
    ).toEqual({ 'Fn::GetAtt': [functionLogicalId, 'FunctionARN'] });
  });

  it('does NOT attach the function to /api/*', () => {
    const apiBehavior = (synthed.distributionConfig.CacheBehaviors ?? []).find(
      (b: any) => b.PathPattern === '/api/*',
    );
    expect(apiBehavior, 'the /api/* behaviour must exist for this assertion to mean anything')
      .toBeDefined();
    expect(
      apiBehavior.FunctionAssociations,
      'the /api/* behaviour has a FunctionAssociations entry.\n' +
        '\n' +
        'DO NOT ATTACH the SPA router to /api/*. It rewrites request.uri to /index.html for any ' +
        'extensionless path, and every API route is extensionless — /api/tests/{id}, ' +
        '/api/pipelines, /api/approvals. Attaching it here would send "/index.html" to API ' +
        'Gateway instead of the real path, which is a worse version of the distribution-wide ' +
        'errorResponses defect this change removed. Per-behaviour attachment is the ONLY reason ' +
        'this mechanism is safe where errorResponses was not.',
    ).toBeUndefined();
  });

  it('attaches the function to no behaviour other than the default', () => {
    // Stated separately from the /api/* case so a future third behaviour cannot
    // pick the function up unnoticed.
    for (const behavior of (synthed.distributionConfig.CacheBehaviors ?? []) as any[]) {
      expect(
        behavior.FunctionAssociations,
        `behaviour '${behavior.PathPattern}' carries a FunctionAssociations entry. The SPA ` +
          'rewrite belongs on the S3 default behaviour only — any additional behaviour is a ' +
          'non-S3 origin whose request URIs must not be rewritten.',
      ).toBeUndefined();
    }
  });

  // ── Documented service bounds, asserted against the RENDERED resource ─────
  //
  // Read off the synthesized template rather than off `lib/spa-router-function.ts`
  // because what fails is what CloudFormation submits. A source scan would also
  // pass on a value CDK renders differently — `LGR-1166391-1` (template /
  // ratified) settles this for CDK-shape guards on exactly that ground: "a source
  // scan can pass on input that still fails at deploy".
  describe('the strings handed to CloudFront stay inside their documented limits', () => {
    it('keeps FunctionConfig.Comment within the documented 128-character limit', () => {
      const comment = functionResource?.Properties?.FunctionConfig?.Comment;

      // Guards the bound below against a vacuous pass: `undefined.length` would
      // throw rather than assert, and a missing Comment is itself a deploy
      // failure since FunctionConfig.Comment is Required: Yes.
      expect(
        typeof comment,
        'FunctionConfig.Comment did not render as a string. It is a REQUIRED property of ' +
          'AWS::CloudFront::Function, so its absence is a deploy failure in its own right — and ' +
          'it leaves the length bound below untested.',
      ).toBe('string');

      expect(
        comment.length,
        `FunctionConfig.Comment is ${comment.length} characters. CloudFront rejects anything over ` +
          `${CLOUDFRONT_FUNCTION_COMMENT_MAX_CHARS}.\n` +
          '\n' +
          'THIS IS A DEPLOY-TIME REJECTION, NOT A STYLE RULE. If you are reading this, the ' +
          'assertion has just saved you a stack rollback: `cdk synth` accepts an over-length ' +
          'comment, every other suite in the repo stays green, and the failure arrives from the ' +
          'CloudFront API during CREATE as\n' +
          '\n' +
          '    CREATE_FAILED | AWS::CloudFront::Function | SpaRouterFunction95E96187\n' +
          '    "Invalid request provided: AWS::CloudFront::Function: The parameter Comment is\n' +
          '     too big. (Service: CloudFront, Status Code: 400)"\n' +
          '\n' +
          'which is exactly what happened to AgenticDataIngestion-Hosting-dev on 2026-08-31 at ' +
          '136 characters.\n' +
          '\n' +
          'THE FIX IS TO SHORTEN THE COMMENT, not to raise this number — 128 is the documented ' +
          'service limit, not a local preference. The comment is a console LABEL; the full ' +
          'reasoning belongs in the module docstring of lib/spa-router-function.ts, which has no ' +
          'length limit and is where a reader actually goes. Leave headroom while you are here.',
      ).toBeLessThanOrEqual(CLOUDFRONT_FUNCTION_COMMENT_MAX_CHARS);
    });

    it('keeps the function Name within its documented length and charset', () => {
      const name = functionResource?.Properties?.Name;

      expect(
        typeof name,
        'Name did not render as a string — either the property is gone (it is Required: Yes) or ' +
          'it renders as an unresolved intrinsic, in which case neither bound below is being ' +
          'tested and this assertion needs rethinking rather than deleting.',
      ).toBe('string');

      expect(
        name.length,
        `The function Name is ${name.length} characters. CloudFront documents a maximum of ` +
          `${CLOUDFRONT_FUNCTION_NAME_MAX_CHARS}, and like the Comment above this is a ` +
          'DEPLOY-TIME rejection that synth does not see — worse, in fact, because Name updates ' +
          'require REPLACEMENT, so an over-length name fails on the way to replacing a live ' +
          'function.\n' +
          '\n' +
          `Rendered: '${name}'. The name is account- and region-scoped on purpose ` +
          '(publication-cleanup R11: CloudFront Function names are unique per account, so an ' +
          'unscoped deterministic name collides across deployments) — so the fix is to shorten ' +
          'the PREFIX or the environment suffix, not to drop the scoping.',
      ).toBeLessThanOrEqual(CLOUDFRONT_FUNCTION_NAME_MAX_CHARS);

      expect(
        name,
        `The function Name '${name}' contains characters outside the documented pattern ` +
          '[a-zA-Z0-9-_]{1,64}. The likeliest cause is an `environment` value carrying a dot, a ' +
          'slash or a space, since the rest of the name is literal, the account is digits and the ' +
          'region is already within the charset. CloudFront rejects this at CREATE; synth does ' +
          'not check it.',
      ).toMatch(CLOUDFRONT_FUNCTION_NAME_PATTERN);
    });

    it('keeps FunctionCode within the documented 10 KB function-size quota', () => {
      const code = functionResource?.Properties?.FunctionCode;

      expect(
        typeof code,
        'FunctionCode did not render as a string. It is Required: Yes, and without it the size ' +
          'bound below is untested.',
      ).toBe('string');

      const bytes = Buffer.byteLength(code, 'utf8');
      expect(
        bytes,
        `FunctionCode is ${bytes} UTF-8 bytes, over the ${CLOUDFRONT_FUNCTION_CODE_MAX_BYTES}-byte ` +
          'budget this file holds against CloudFront\'s documented 10 KB maximum function size ' +
          '(not adjustable). A third DEPLOY-TIME rejection synth does not see.\n' +
          '\n' +
          'If you have hit this while narrowing the extension heuristic to an allowlist — the ' +
          "revisit_when of LGR-4d242bf-1 and of this construct's docstring — the code genuinely " +
          'needs to shrink: move the allowlist into a CloudFront KeyValueStore rather than ' +
          'inlining it, which is what the quota note points at. Do NOT raise the constant; 10 KB ' +
          'is the service limit and this file already takes the stricter 10 x 10^3 reading of it.',
      ).toBeLessThanOrEqual(CLOUDFRONT_FUNCTION_CODE_MAX_BYTES);
    });
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// The function body itself, compiled and executed
// ─────────────────────────────────────────────────────────────────────────────

type CfRequest = { uri: string };
type CfHandler = (event: { request: CfRequest }) => CfRequest;

/**
 * Compile the deployed source string and hand back its `handler`.
 *
 * `new Function` over `SPA_ROUTER_FUNCTION_CODE` means the logic under test is
 * byte-for-byte the logic CloudFront runs — there is no second implementation to
 * drift from it.
 */
function loadHandler(): CfHandler {
  // eslint-disable-next-line @typescript-eslint/no-implied-eval
  return new Function(`${SPA_ROUTER_FUNCTION_CODE}\nreturn handler;`)() as CfHandler;
}

/** Run the handler and return the URI the origin would receive. */
function resolve(handler: CfHandler, uri: string): string {
  return handler({ request: { uri } }).uri;
}

describe('spa-router function — the extensionless rewrite, over real paths', () => {
  let handler: CfHandler;

  beforeAll(() => {
    handler = loadHandler();
  });

  it('compiles and exposes a handler', () => {
    expect(typeof handler).toBe('function');
  });

  // ── Hazard: /config.json must keep resolving, or the app cannot boot ──────
  it('passes /config.json through untouched', () => {
    // `/config.json` is a REAL S3 object, written by BucketDeployment via
    // `s3deploy.Source.jsonData('config.json', …)` in hosting-stack.ts, and
    // fetched at startup by `config.ts:103` for region, userPoolId,
    // userPoolClientId, hostedUiDomain, apiBaseUrl and wsUrl. Rewriting it to
    // /index.html makes `loadConfig()` fail to parse and the app never boots —
    // `main.tsx:70` renders the "Check that /config.json is available" screen.
    // The extension check is what protects it; this asserts that rather than
    // trusting it.
    expect(resolve(handler, '/config.json')).toBe('/config.json');
  });

  // ── Hazard: hashed build assets must keep resolving ───────────────────────
  it.each([
    '/assets/index-DkX3a9Zq.js',
    '/assets/index-B7fK2mQ1.css',
    '/assets/vendor-a1b2c3d4.js',
    '/assets/logo-9f8e7d6c.svg',
    '/assets/inter-latin-400.woff2',
    '/assets/inter-latin-400.woff',
    '/assets/glyphs.ttf',
    '/favicon.ico',
    '/vite.svg',
    '/index.html',
    '/robots.txt',
    '/apple-touch-icon.png',
  ])('passes the static asset %s through untouched', (uri) => {
    expect(resolve(handler, uri)).toBe(uri);
  });

  // ── Hazard: every legitimate SPA route must reach index.html ──────────────
  // Enumerated from App.tsx's <Route> table (:109, :147-164) plus the nested
  // /pipelines/:id children (:150-156). With errorResponses gone, a route
  // missing from this rewrite 403s from S3 rather than falling back — so this
  // list is the regression guard for the behaviour change.
  it.each([
    '/',
    '/pipelines',
    '/pipelines/new',
    '/pipelines/orders-a1b2c3d4',
    '/pipelines/orders-a1b2c3d4/schema',
    '/pipelines/orders-a1b2c3d4/code',
    '/pipelines/orders-a1b2c3d4/executions',
    '/pipelines/orders-a1b2c3d4/files',
    '/pipelines/orders-a1b2c3d4/mapping-memory',
    '/pipelines/orders-a1b2c3d4/runs/run-20260828-0001',
    '/pipelines/orders-a1b2c3d4/glue-job/agentic-data-ingestion-job-orders',
    '/approvals',
    '/schemas',
    '/archived',
    '/metrics',
    '/errors',
    '/chat',
  ])('rewrites the SPA route %s to /index.html', (uri) => {
    expect(resolve(handler, uri)).toBe('/index.html');
  });

  it('rewrites /auth/callback — the Cognito redirect target', () => {
    // Called out separately because it is the one route whose failure is an auth
    // dead end rather than a blank page: it is a registered Cognito callback URL
    // (`hosting-stack.ts`, `${cloudFrontUrl}/auth/callback`) and it is
    // extensionless, so it MUST reach index.html for the OIDC code exchange in
    // `AuthCallback` to run. Send it to S3 and login completes nowhere.
    expect(resolve(handler, '/auth/callback')).toBe('/index.html');
  });

  // ── Hazard: an id containing a dot ───────────────────────────────────────
  it('rewrites a route whose id carries a digit-suffixed dot', () => {
    // The case a bare "contains a dot" heuristic gets wrong. `28` is not an
    // extension because it does not begin with a letter, so this stays an SPA
    // route. No web asset extension is purely numeric.
    expect(resolve(handler, '/pipelines/release-2026.08.28')).toBe('/index.html');
    expect(resolve(handler, '/pipelines/release-2026.08.28/code')).toBe('/index.html');
  });

  it('ignores a dot that is not in the final path segment', () => {
    // The other half of the same hazard, and the reason the check is scoped to
    // the last segment: a dotted id followed by a further segment must not be
    // read as a file at all.
    expect(resolve(handler, '/pipelines/orders.v2/code')).toBe('/index.html');
    expect(resolve(handler, '/pipelines/orders.v2/runs/run-1')).toBe('/index.html');
  });

  it('sends a final segment with a letter-suffixed dot to S3 — the accepted residual', () => {
    // ASSERTED AS A KNOWN LIMIT, NOT AS DESIRED BEHAVIOUR. `v2` looks exactly
    // like an extension, so this reaches S3 and answers 403.
    //
    // Accepted on evidence: pipeline ids CANNOT contain a dot.
    // `handlers/pipelines_crud.py:1892-1894` builds the id from the pipeline
    // name, filters it with `c.isalnum() or c == "-"` — dropping every dot — and
    // appends 8 hex characters of a uuid4. So this shape is unreachable through
    // the product's own id generation and only reachable by a hand-typed URL,
    // which would fail to resolve inside the SPA anyway.
    //
    // If a route with this shape ever becomes real, the fix is to narrow the
    // extension test in `lib/spa-router-function.ts` (an allowlist of the
    // extensions the build actually emits) and to flip this assertion — NOT to
    // reintroduce errorResponses.
    expect(resolve(handler, '/pipelines/orders.v2')).toBe('/pipelines/orders.v2');
  });

  // ── Boundary properties of the extension test ────────────────────────────
  it('does not treat a long word-like suffix as an extension', () => {
    // The 8-character ceiling. `woff2` (5) is the longest this build emits.
    expect(resolve(handler, '/pipelines/orders.production')).toBe('/index.html');
  });

  it('does not treat a leading dot as an extension', () => {
    // `lastDot > 0`, so a dotfile-shaped segment is a route, not a file. This
    // origin serves no dotfiles.
    expect(resolve(handler, '/.env')).toBe('/index.html');
  });

  it('does not treat a non-alphanumeric suffix as an extension', () => {
    expect(resolve(handler, '/pipelines/orders.v-2')).toBe('/index.html');
    expect(resolve(handler, '/pipelines/orders.')).toBe('/index.html');
  });

  it('is idempotent on /index.html itself', () => {
    // The rewrite target must not be rewritten again, or a second pass could
    // loop. It has an extension, so it passes through.
    expect(resolve(handler, '/index.html')).toBe('/index.html');
    expect(resolve(handler, resolve(handler, '/pipelines'))).toBe('/index.html');
  });

  it('leaves the request object otherwise intact', () => {
    // CloudFront requires the handler to return the request it was given, with
    // headers/querystring preserved; returning a fresh object would drop them.
    const request = { uri: '/pipelines', headers: { host: { value: 'd123.cloudfront.net' } } };
    const returned = handler({ request } as any) as any;
    expect(returned).toBe(request);
    expect(returned.headers.host.value).toBe('d123.cloudfront.net');
  });
});
