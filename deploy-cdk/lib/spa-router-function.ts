// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The SPA deep-link rewrite, as a CloudFront Function on viewer-request.
 *
 * ## Why this exists rather than `Distribution.errorResponses`
 *
 * The Hosting distribution previously carried distribution-wide
 * `errorResponses` mapping **403 and 404 to HTTP 200 with the body of
 * `/index.html`**. CloudFront custom error responses cannot be scoped to a
 * behaviour — they are a property of `DistributionConfig`, not of
 * `DefaultCacheBehavior` — so the SPA fallback also rewrote every error the
 * `/api/*` behaviour produced. The consequences, each verified in the tree:
 *
 * 1. `GET /api/tests/{codeVersionId}` answers **404** when a code version has no
 *    test-results rows yet (`handlers/pipelines_artifacts.py:89`,
 *    `create_response(404, …)`).
 * 2. The distribution rewrote it to **200 + index.html**, so the client received
 *    HTML under a success status.
 * 3. `api-client._unwrap` reads the status as the classifier, `.json()` throws on
 *    HTML, and `res.ok` is true — so it raises
 *    `ApiError(200, 'Response body could not be read as JSON (HTTP 200)')`
 *    (`services/api-client.ts:190-194`). That reaches the operator as a toast.
 * 4. `fetchTestResults` converts only a **404** into the friendly pending state
 *    (`hooks/use-code.ts:56`). The 404 no longer existed, so it rethrew, and
 *    `TestResultsViewer` rendered *"Failed to load test results."* with a Retry
 *    that could never succeed.
 *
 * The same rewrite applied to **403**, so a WAF block or a Cognito authorizer
 * rejection on `/api/*` also arrived as a non-JSON 200. The fix is therefore
 * broader than the Code tab: `errorResponses` is removed entirely and the SPA
 * fallback is expressed as a viewer-request rewrite attached to the **default
 * (S3) behaviour only**, which is a per-behaviour association and so cannot
 * reach `/api/*`.
 *
 * ## The extensionless heuristic, and its one residual
 *
 * `handler` treats a request as a static asset — and passes it through to S3
 * untouched — when the **final** path segment carries something that looks like a
 * file extension: a dot at position > 0, followed by 1–8 alphanumerics beginning
 * with an **ASCII letter**. Everything else is rewritten to `/index.html`.
 *
 * Three properties of that shape are deliberate:
 *
 * - **Final segment only.** A dot anywhere earlier is ignored, so
 *   `/pipelines/orders.v2/code` still reaches the SPA. A bare "the path contains
 *   a dot" test is the naive version of this heuristic and misroutes exactly that
 *   case.
 * - **Must begin with a letter.** This is what keeps a dotted, digit-suffixed id
 *   on the SPA side: `/pipelines/release-2026.08.28` has the suffix `28`, which
 *   is not an extension, so it is rewritten. No web asset extension is purely
 *   numeric.
 * - **At most 8 characters.** `woff2` (5) is the longest this repo's build emits;
 *   8 leaves headroom without letting a long word-like segment read as an
 *   extension.
 *
 * **The residual, stated rather than discovered later:** a final path segment
 * whose dot-suffix *does* begin with a letter is sent to S3 and answers 403 —
 * `/pipelines/orders.v2` is the realistic example. This is accepted, and it is
 * accepted on evidence rather than optimism: pipeline ids cannot contain a dot.
 * `handlers/pipelines_crud.py:1892-1894` derives the id from the pipeline name,
 * filters it with `c.isalnum() or c == "-"` — which drops every dot — and then
 * appends 8 hex characters of a uuid4. So the case is unreachable through the
 * product's own id generation and is only reachable by a hand-typed URL, which
 * would fail to resolve inside the SPA anyway. Both halves are pinned by test in
 * `test/p56-spa-router-function.test.ts` so the boundary stays visible.
 *
 * ## Behaviour change from dropping `errorResponses`
 *
 * A genuinely missing S3 object used to return `index.html` with 200. It now
 * returns S3's own 403/404. That is the intended direction — a missing asset must
 * not masquerade as a working app — but it means the rewrite above has to cover
 * every legitimate SPA route, which is what the route table in the test file is
 * for.
 */
import * as cdk from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import { Construct } from 'constructs';

/**
 * The exact source handed to CloudFront.
 *
 * Written as ES5-compatible JavaScript with `var` and no arrow functions: this
 * string is the deployed artifact, not TypeScript that gets compiled, so nothing
 * transpiles it. `test/p56-spa-router-function.test.ts` compiles **this same
 * string** with `new Function` and exercises the resulting `handler`, so the
 * tested logic cannot drift from the deployed logic.
 */
export const SPA_ROUTER_FUNCTION_CODE = `
function handler(event) {
  var request = event.request;
  var uri = request.uri;

  // Only the FINAL path segment can carry an extension. A dot in an earlier
  // segment belongs to a route parameter (a pipeline or run id), not to a file.
  var lastSlash = uri.lastIndexOf('/');
  var lastSegment = lastSlash === -1 ? uri : uri.substring(lastSlash + 1);

  // lastDot > 0 rather than >= 0: a leading dot is a dotfile, not an extension,
  // and this origin serves none.
  var lastDot = lastSegment.lastIndexOf('.');
  if (lastDot > 0) {
    var ext = lastSegment.substring(lastDot + 1);
    // Must begin with an ASCII letter, so a digit-suffixed id such as
    // 'release-2026.08.28' is a route rather than a '.28' file.
    if (/^[A-Za-z][A-Za-z0-9]{0,7}$/.test(ext)) {
      return request;
    }
  }

  // Extensionless: an SPA deep link. Serve index.html and let the router decide.
  request.uri = '/index.html';
  return request;
}
`;

export interface SpaRouterFunctionProps {
  /** Deployment environment, used only to name the function. */
  readonly environment: string;
}

/**
 * A `cloudfront.Function` carrying {@link SPA_ROUTER_FUNCTION_CODE}.
 *
 * A CloudFront Function rather than Lambda@Edge, deliberately: a Function needs
 * no execution role and no us-east-1 replica, so it adds no IAM surface and no
 * new cdk-nag findings — `lib/nag-acknowledgments.ts` is a GENERATED file and
 * needs no entry for this construct. Lambda@Edge would have required both.
 */
export class SpaRouterFunction extends Construct {
  public readonly function: cloudfront.Function;

  constructor(scope: Construct, id: string, props: SpaRouterFunctionProps) {
    super(scope, id);

    const stack = cdk.Stack.of(this);

    this.function = new cloudfront.Function(this, 'Function', {
      // Account+region scoped for the same reason the site bucket is
      // (publication-cleanup R11): CloudFront Function names are unique per
      // account, and a deterministic unscoped name collides across deployments.
      functionName: `adi-spa-router-${stack.account}-${stack.region}-${props.environment}`,
      // A LABEL, NOT THE REASONING — and hard-capped at 128 characters by
      // CloudFront. The module docstring above carries the full argument and is
      // where a reader goes; this is the one line an operator sees in the
      // console. The previous wording was 136 characters and failed the deploy
      // (`The parameter Comment is too big`, CREATE_FAILED, 2026-08-31), which is
      // why `test/p56-spa-router-function.test.ts` now asserts the bound against
      // the synthesized template. Keep an edit here comfortably short; the test
      // will tell you if you don't.
      comment:
        'Rewrites extensionless paths to /index.html (SPA deep links). Replaced errorResponses, so /api/* errors survive.',
      code: cloudfront.FunctionCode.fromInline(SPA_ROUTER_FUNCTION_CODE),
      runtime: cloudfront.FunctionRuntime.JS_2_0,
    });
  }
}
