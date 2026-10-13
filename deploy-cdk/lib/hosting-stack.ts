// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import { execSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { Construct } from 'constructs';
import { SpaRouterFunction } from './spa-router-function';
import { NODE20_BUILD_IMAGE, pinnedBuildImage } from './build-images';
import { retainLambdaLogs } from './log-retention';

/**
 * Bundle the React dashboard as part of `cdk synth`/`cdk deploy` (P27 Req 6).
 *
 * The asset source is the dashboard SOURCE directory (not a pre-built dist/),
 * so a fresh clone deploys with no manual `npm run build` step. The asset
 * fingerprint covers the source (minus excludes), so the build re-runs exactly
 * when dashboard source or the lockfile changes and hits the CDK asset cache
 * otherwise.
 *
 * Local-first: if Node is available on the host, build in-place in
 * dashboard-react/ (fast). A local BUILD failure propagates — it must fail the
 * synth, not silently fall back to Docker (Req 6.3). Docker is used only when
 * host Node is unavailable. In Docker mode the source is bind-mounted, so the
 * container copies it to a temp dir first — running `npm ci` directly in
 * /asset-input would delete the host's node_modules through the mount.
 */
function dashboardBundledAsset(dashboardDir: string): s3deploy.ISource {
  return s3deploy.Source.asset(dashboardDir, {
    // Fingerprint-only: bundling bind-mounts the unfiltered dashboardDir at
    // /asset-input (and the local path builds in dashboardDir directly), so
    // nothing here can starve `npm ci`. What it does control is when the
    // dashboard is rebuilt — dashboard-react/.venv alone was 36 MB of Python
    // virtualenv being hashed on every synth to decide whether a TypeScript
    // bundle needed rebuilding.
    ignoreMode: cdk.IgnoreMode.GIT,
    exclude: [
      'node_modules', 'dist', 'coverage', '.env*', '*.log',
      // Python virtualenv and caches that live alongside the React app.
      '.venv', 'venv', '__pycache__', '*.pyc',
      '.mypy_cache', '.ruff_cache', '.pytest_cache',
      // Incremental-build and test-run output.
      '*.tsbuildinfo', 'test-results', 'playwright-report', '.vite',
      '.DS_Store', '.dashboard.pids',
    ],
    bundling: {
      image: pinnedBuildImage(NODE20_BUILD_IMAGE),
      // CDK runs the container as the host UID, whose HOME does not exist in
      // the image — point HOME and the npm cache at /tmp or npm fails EACCES.
      command: ['bash', '-c', [
        'export HOME=/tmp',
        'mkdir -p /tmp/app',
        'tar cf - --exclude=node_modules --exclude=dist -C /asset-input . | tar xf - -C /tmp/app',
        'cd /tmp/app',
        'npm ci --cache /tmp/.npm',
        'npm run build',
        'cp -r dist/* /asset-output/',
      ].join(' && ')],
      local: {
        tryBundle(outputDir: string): boolean {
          try {
            execSync('node --version', { stdio: 'ignore' });
          } catch {
            return false; // no host Node → Docker fallback
          }
          // Build failures below throw and fail the synth with npm's output.
          execSync('npm ci && npm run build', { cwd: dashboardDir, stdio: 'inherit' });
          fs.cpSync(path.join(dashboardDir, 'dist'), outputDir, { recursive: true });
          return true;
        },
      },
    },
  });
}

export interface HostingStackProps extends cdk.StackProps {
  environment: string;
  /** REST API execute-api hostname (e.g. {restApiId}.execute-api.{region}.amazonaws.com). When provided, a /api/* behavior is added. */
  restApiHost?: string;
  /** The Cognito User Pool ID (for config.json). */
  userPoolId?: string;
  /** The Cognito User Pool ARN (to import the pool and create the app client). */
  userPoolArn?: string;
  /** The Cognito Hosted UI domain URL. */
  hostedUiDomain?: string;
  /** The WebSocket API URL. */
  wsUrl?: string;
  /** Plaintext secret value for X-Origin-Verify header (from Secrets Manager, passed as prop to avoid cross-stack construct refs). */
  originVerifySecretValue?: string;
  /** ARN of the CLOUDFRONT-scope WAF web ACL (created in the us-east-1 EdgeWafStack, passed via cross-region references). When undefined, no WAF is attached. */
  cloudFrontWebAclArn?: string;
  /**
   * SSM parameter this stack writes the dashboard app client ID to, for the
   * WebSocket authorizer in the Compute stack to read (R-05). Compute deploys
   * before Hosting and cannot reference the client directly without a cycle.
   * When undefined, no parameter is written.
   */
  dashboardClientIdParameterName?: string;
}

export class HostingStack extends cdk.Stack {
  public readonly distribution: cloudfront.Distribution;
  public readonly siteBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: HostingStackProps) {
    super(scope, id, props);

    const { environment, restApiHost, userPoolId, userPoolArn, hostedUiDomain, wsUrl, originVerifySecretValue } = props;

    // ── Local-development allowances (threat model T-16, R-12) ─────────
    // `-c adi:allowLocalhost=true` adds http://localhost:5173 to the Cognito
    // callback and logout URLs and to the /api/* CORS origins, for the dev
    // modes in docs/LOCAL_DEVELOPMENT.md. Off by default, and all three move
    // together so the two concessions cannot drift apart (LGR-f34fe5c-3's
    // revisit condition). Gated on a flag rather than on `environment`, as that
    // record proposed, because every deployment is `dev` unless told otherwise:
    // an environment gate would have left localhost on in every deployment.
    const allowLocalhost =
      this.node.tryGetContext('adi:allowLocalhost') === 'true' ||
      this.node.tryGetContext('adi:allowLocalhost') === true;
    const LOCAL_DEV_ORIGIN = 'http://localhost:5173';

    // ── Browser security headers (threat model T-16, R-12) ─────────────
    // Applied by CloudFront to every response on both behaviours. The SPA
    // gets a Content-Security-Policy listing exactly the origins it talks to;
    // /api/* responses are JSON and get a CSP that permits nothing.
    //
    // What the dashboard needs, from the built bundle and its source:
    //   script-src 'self'    one module script from /assets; no inline script,
    //                        no eval or new Function in the bundle.
    //   style-src 'unsafe-inline'  TestResultsViewer's markdown renderer and
    //                        the main.tsx boot-error page emit style="..."
    //                        attributes. Removing it means rewriting those.
    //   font-src data:       Cloudscape's fonts are inlined in the CSS.
    //   connect-src          /api and /config.json ('self'); the user pool's
    //                        OIDC metadata and JWKS (cognito-idp); the Hosted
    //                        UI's token endpoint; the WebSocket API; and the
    //                        source bucket, which FileUploadModal POSTs to with
    //                        a presigned form and HEADs to confirm. Both S3
    //                        host forms are listed because botocore picks one.
    //   frame-src            the Hosted UI, for oidc-client-ts's silent renew.
    const sourceBucketName = `adi-source-files-${this.account}-${this.region}`;
    const wsOrigin = wsUrl ? `wss://${cdk.Fn.select(2, cdk.Fn.split('/', wsUrl))}` : undefined;
    const connectSources = [
      "'self'",
      `https://cognito-idp.${this.region}.amazonaws.com`,
      ...(hostedUiDomain ? [hostedUiDomain] : []),
      ...(wsOrigin ? [wsOrigin] : []),
      `https://${sourceBucketName}.s3.amazonaws.com`,
      `https://${sourceBucketName}.s3.${this.region}.amazonaws.com`,
    ];
    const spaContentSecurityPolicy = [
      "default-src 'self'",
      "script-src 'self'",
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data:",
      "font-src 'self' data:",
      `connect-src ${connectSources.join(' ')}`,
      `frame-src ${hostedUiDomain ?? "'none'"}`,
      "frame-ancestors 'none'",
      "form-action 'self'",
      "base-uri 'self'",
      "object-src 'none'",
    ].join('; ');
    const securityHeaders = (contentSecurityPolicy: string): cloudfront.ResponseSecurityHeadersBehavior => ({
      contentSecurityPolicy: { contentSecurityPolicy, override: true },
      strictTransportSecurity: {
        accessControlMaxAge: cdk.Duration.days(365),
        includeSubdomains: true,
        override: true,
      },
      contentTypeOptions: { override: true },
      frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
      referrerPolicy: {
        referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
        override: true,
      },
    });
    const spaHeadersPolicy = new cloudfront.ResponseHeadersPolicy(this, 'SpaSecurityHeaders', {
      comment: 'R-12 — CSP, HSTS, nosniff, frame and referrer headers for the dashboard',
      securityHeadersBehavior: securityHeaders(spaContentSecurityPolicy),
    });

    // ── S3 Bucket (private, OAC only) ──────────────────────────────
    // Account+region-scoped like every other bucket (adi-*-{account}-{region},
    // publication-cleanup R9.4/R11): S3 bucket names are globally unique across
    // ALL AWS accounts, so a deterministic name without the account ID is
    // claimed by whichever account deploys first and blocks everyone else
    // (observed in the July 2026 external deploy test). Renaming REPLACES the
    // bucket on existing deployments; autoDeleteObjects empties the old one and
    // the BucketDeployment repopulates the new one in the same deploy.
    this.siteBucket = new s3.Bucket(this, 'SiteBucket', {
      bucketName: `adi-dashboard-${this.account}-${this.region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true, // deny non-TLS access (AwsSolutions-S10)
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // ── CloudFront access logs (security review S9) ───────────────
    // The record of who reached the deployment, with the viewer's own IP
    // (the REST access log only sees CloudFront's). Standard (legacy) logging to
    // S3: logging v2 to CloudWatch needs its delivery source in us-east-1, and
    // this stack is regional. Legacy logging writes with the awslogsdelivery
    // account's ACL grant, so this one bucket needs ACLs enabled
    // (BUCKET_OWNER_PREFERRED); it stays private (BLOCK_ALL), SSE-S3 and
    // TLS-only. 30 days, like every log group in the app. The query string is
    // logged but carries nothing sensitive on this distribution: the SPA's OIDC
    // callback code is single-use and PKCE-bound, and the WebSocket token never
    // passes through CloudFront. Cookies are not logged.
    //
    // Named OUTSIDE the `adi-*-{account}-{region}` family on purpose: that
    // family is granted read and write by several Lambda and runtime roles
    // (foundation-stack.ts `s3ReadWrite` and the runtime read grant), and an
    // access log those roles can read or overwrite is not a record of anything.
    // Account and region keep the name globally unique, as for the other buckets.
    const accessLogBucket = new s3.Bucket(this, 'CloudFrontAccessLogs', {
      bucketName: `agentic-data-ingestion-cf-logs-${this.account}-${this.region}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
      lifecycleRules: [{ expiration: cdk.Duration.days(30) }],
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // ── CloudFront WAF (CLOUDFRONT scope) ─────────────────────────
    // A CLOUDFRONT-scope WAF web ACL must live in us-east-1. It is created in
    // the dedicated EdgeWafStack (pinned to us-east-1) and its ARN is passed in
    // via cross-region references as `cloudFrontWebAclArn`.

    // ── /api/* behavior (REST API origin) ──────────────────────────
    // Only added when restApiHost is provided so the stack still synthesizes
    // without the Compute stack deployed.
    const additionalBehaviors: Record<string, cloudfront.BehaviorOptions> = {};

    if (restApiHost) {
      // Inject X-Origin-Verify secret header when the secret value is available
      const customHeaders: Record<string, string> = {};
      if (originVerifySecretValue) {
        customHeaders['X-Origin-Verify'] = originVerifySecretValue;
      }

      const apiOrigin = new origins.HttpOrigin(restApiHost, {
        originPath: '/v1',
        protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        ...(Object.keys(customHeaders).length > 0 && { customHeaders }),
      });

      // ── CORS on /api/*: set at the edge, not in the Lambda (p47 R13.3) ──
      // `shared/response_utils.py` emits `Access-Control-Allow-Origin: *` on all
      // 332 of its call sites. This policy REPLACES that header on the way out
      // (`originOverride: true` — CloudFront ignores the header it received from
      // the origin when the policy carries the same header). R13.2 assumed the
      // origin had to reach the Lambda as an environment variable; it does not have
      // to reach the Lambda at all. See requirements.md R13.2's amendment and
      // e4-cors-origin-arms.md §6.
      //
      // Chosen over a post-deploy `update-function-configuration` (R13's arm 1)
      // because CDK owns this value, so it CANNOT drift: arm 1's variable is reset
      // to its source value by the next `cdk deploy` of Compute — including the
      // `--exclusively` Lambda-code path, the most-run deploy command in this repo
      // — and the symptom is a silently permissive header that nothing fails on.
      //
      // WHY THE DISTRIBUTION'S OWN DOMAIN IS NOT IN THIS LIST. Two reasons, and the
      // first one is a hard blocker, not a preference:
      //
      //   1. It would emit a CloudFormation CIRCULAR DEPENDENCY that `cdk synth`
      //      accepts without complaint. (`this.distribution` is also not yet
      //      assigned here — the Distribution is constructed below, from these
      //      behaviours — so the first symptom is a TypeError. Restructuring to fix
      //      THAT is what walks into the cycle.) The behaviour below carries
      //      `ResponseHeadersPolicyId: { Ref: <this policy> }`, so the policy must
      //      exist first; putting `https://${this.distribution.distributionDomainName}`
      //      in `accessControlAllowOrigins` puts
      //      `Fn::GetAtt: [<Distribution>, DomainName]` inside the policy, so the
      //      distribution must exist first. Verified by synthesizing exactly that
      //      shape: no synth error, and both references present in the template.
      //      Same class of defect as the WAFv2 association race documented in
      //      `compute-stack.ts` — invisible to synth, fatal at deploy.
      //   2. It is not needed. An origin only belongs here if it can ever be a
      //      CROSS-origin caller, and the distribution's own domain never is: the
      //      SPA and the API share scheme, host and port, so those requests carry
      //      no `Origin` header at all. Relative or absolute `apiBaseUrl` makes no
      //      difference — an origin is scheme+host+port.
      //
      // WHY localhost IS in the list (p47 R13.4, decided rather than inherited).
      // `docs/LOCAL_DEVELOPMENT.md` §4 documents an alternative dev mode —
      // `VITE_API_BASE_URL=https://d….cloudfront.net/api`, no Vite proxy — that IS
      // genuinely cross-origin from `http://localhost:5173`, and it works today
      // only because the Lambda answers `*`. Narrowing without this entry would
      // break that documented mode, and it would break it as a confusing CORS
      // failure weeks later rather than at deploy. The recommended proxy mode
      // (`VITE_API_PROXY_TARGET`, `vite.config.ts`) is same-origin and unaffected
      // either way.
      //   Its cost, stated: a page served from any localhost:5173 can read `/api/*`
      // responses cross-origin. That page still cannot obtain a token
      // (`sessionStorage`, explicit header), so its requests are unauthenticated
      // and the Cognito authorizer answers 401 — the same argument that makes the
      // Lambda's `*` harmless, applied to a much smaller surface.
      //   GATED, since R-12, on `adi:allowLocalhost` together with the two
      // Cognito localhost URLs below (see `allowLocalhost` above), so the two
      // concessions still move as one. A callback URL receives an authorization
      // code, which is strictly more than CORS read access; gating one and not
      // the other would give false comfort.
      //
      // With the flag off there is no origin to allow, and CloudFront needs at
      // least one in a CORS config, so the policy carries no corsBehavior and
      // REMOVES Access-Control-Allow-Origin instead. That strips the Lambda's `*`
      // and API Gateway's preflight `*` alike, so no other origin can read
      // /api/* responses at all. Same mechanism, stricter result.
      const apiCorsBehavior: cloudfront.ResponseHeadersCorsBehavior = {
        // Kept identical to what `shared/response_utils.py` emits, so the edge
        // and the origin do not disagree about anything except the origin list.
        accessControlAllowHeaders: [
          'Content-Type',
          'X-Amz-Date',
          'Authorization',
          'X-Api-Key',
          'X-Amz-Security-Token',
        ],
        accessControlAllowMethods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
        accessControlAllowOrigins: [LOCAL_DEV_ORIGIN],
        // MUST stay false. `Access-Control-Allow-Credentials: true` is
        // spec-incompatible with a wildcard origin, the header is absent from
        // this entire repository, and no request here carries a cookie — the
        // Cognito token travels in an explicit `Authorization` header.
        accessControlAllowCredentials: false,
        // Replace the origin's header rather than pass it through. This is the
        // whole mechanism: without it the Lambda's `*` reaches the browser.
        originOverride: true,
        // No `accessControlMaxAge`: `response_utils.py` emits no
        // `Access-Control-Max-Age`, and adding one here would be the edge inventing
        // a preflight cache lifetime the origin never asked for.
      };
      const apiCorsPolicy = new cloudfront.ResponseHeadersPolicy(this, 'ApiCorsHeaders', {
        comment: allowLocalhost
          ? 'p47 R13 — narrows Access-Control-Allow-Origin on /api/* at the edge'
          : 'p47 R13 / R-12 — strips Access-Control-Allow-Origin on /api/* at the edge',
        ...(allowLocalhost
          ? { corsBehavior: apiCorsBehavior }
          : { removeHeaders: ['Access-Control-Allow-Origin'] }),
        // JSON responses: a CSP that allows nothing, plus the same transport,
        // sniffing, framing and referrer headers as the SPA.
        securityHeadersBehavior: securityHeaders("default-src 'none'; frame-ancestors 'none'"),
      });
      additionalBehaviors['/api/*'] = {
        origin: apiOrigin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        // Covers API Gateway's own MOCK OPTIONS preflight too, which is a second
        // `*` surface (Compute's defaultCorsPreflightOptions is Cors.ALL_ORIGINS,
        // because Compute deploys before this stack and cannot know its domain;
        // this policy is the one place the origin is narrowed). A
        // preflight OPTIONS request matches this same `/api/*` path pattern and
        // `allowedMethods` below includes OPTIONS, so the response it produces
        // passes through this policy — and Allow-Methods/Allow-Headers above are
        // exactly the headers CloudFront uses on preflight responses.
        responseHeadersPolicy: apiCorsPolicy,
        // Caching disabled for the API. CACHING_DISABLED forbids cache-key
        // parameters, so the Authorization header + query strings + cookies are
        // forwarded to the origin via the managed ALL_VIEWER_EXCEPT_HOST_HEADER
        // origin-request policy. That policy also drops the viewer Host header,
        // which is required for the API Gateway HttpOrigin to resolve.
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      };
    }

    // ── SPA deep-link rewrite (viewer-request, DEFAULT behaviour only) ──
    // This replaces the distribution-wide `errorResponses` that used to provide
    // the SPA fallback. See `lib/spa-router-function.ts` for the full reasoning;
    // the short version is that CloudFront custom error responses are a property
    // of `DistributionConfig` and CANNOT be scoped per behaviour, so the 403/404
    // → 200 + /index.html mapping also swallowed every error the `/api/*`
    // behaviour produced — turning a real `404 no test results yet`
    // (`handlers/pipelines_artifacts.py:89`) into an HTML body under a 200, which
    // `api-client._unwrap` can only report as
    // `ApiError(200, 'Response body could not be read as JSON')`
    // (`services/api-client.ts:190-194`).
    //
    // DO NOT reintroduce `errorResponses` on this distribution. A function
    // association is per-behaviour and is the only mechanism that gives the SPA
    // its fallback without also rewriting `/api/*`. Pinned by
    // `test/p56-spa-router-function.test.ts`.
    //
    // DO NOT attach this function to `additionalBehaviors['/api/*']` either — it
    // would rewrite API request URIs to `/index.html` before they reached API
    // Gateway, which is a worse version of the same defect.
    const spaRouter = new SpaRouterFunction(this, 'SpaRouter', { environment });

    // ── CloudFront Distribution ────────────────────────────────────
    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: `AgenticDataIngestion Dashboard (${environment})`,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.siteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        responseHeadersPolicy: spaHeadersPolicy,
        functionAssociations: [
          {
            function: spaRouter.function,
            eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
          },
        ],
      },
      additionalBehaviors,
      defaultRootObject: 'index.html',
      // Associate WAF web ACL with the distribution (CLOUDFRONT scope).
      // Sourced from the us-east-1 EdgeWafStack via cross-region references;
      // when undefined, no WAF is attached.
      webAclId: props.cloudFrontWebAclArn,
      enableLogging: true,
      logBucket: accessLogBucket,
      logFilePrefix: 'cloudfront/',
      logIncludesCookies: false,
      // NO `errorResponses` — see the block above. One consequence is real and
      // intended: a genuinely missing S3 object now returns S3's own 403/404
      // instead of index.html with 200. A missing asset must not masquerade as a
      // working app, and every legitimate SPA route is covered by the rewrite.
    });

    // ── Cognito App Client (conditional on userPoolArn) ───────────
    if (userPoolArn) {
      const userPool = cognito.UserPool.fromUserPoolArn(this, 'ImportedUserPool', userPoolArn);

      const cloudFrontUrl = `https://${this.distribution.distributionDomainName}`;

      const appClient = new cognito.UserPoolClient(this, 'UserPoolClient', {
        userPool,
        generateSecret: false,
        // Security review S7. The sign-in endpoints answer "no such user" and
        // "wrong password" alike, so the pool cannot be used to enumerate
        // usernames.
        preventUserExistenceErrors: true,
        // Token lifetimes set explicitly rather than left to Cognito's defaults
        // (1 h / 1 h / 30 days). Access and ID tokens stay at one hour; silent
        // renew (AuthProvider.tsx, automaticSilentRenew) refreshes them. The
        // refresh token is 12 hours rather than 30 days: an operator console is
        // signed into once a working day, and a refresh token copied out of a
        // browser session is then usable for half a day, not a month.
        accessTokenValidity: cdk.Duration.hours(1),
        idTokenValidity: cdk.Duration.hours(1),
        refreshTokenValidity: cdk.Duration.hours(12),
        oAuth: {
          flows: {
            authorizationCodeGrant: true,
            implicitCodeGrant: false,
            clientCredentials: false,
          },
          scopes: [
            cognito.OAuthScope.OPENID,
            cognito.OAuthScope.EMAIL,
            cognito.OAuthScope.PROFILE,
          ],
          // The localhost URLs exist only with -c adi:allowLocalhost=true, gated
          // together with the /api/* CORS origin above (R-12).
          callbackUrls: [
            `${cloudFrontUrl}/auth/callback`,
            ...(allowLocalhost ? [`${LOCAL_DEV_ORIGIN}/auth/callback`] : []),
          ],
          logoutUrls: [
            cloudFrontUrl,
            ...(allowLocalhost ? [LOCAL_DEV_ORIGIN] : []),
          ],
        },
        supportedIdentityProviders: [
          cognito.UserPoolClientIdentityProvider.COGNITO,
        ],
      });

      // ── BucketDeployment: auto-built dashboard + config.json ──────
      // The dashboard is built during synth via asset bundling (P27 Req 6) —
      // no manual `npm run build` step. config.json stays a separate
      // deploy-time source; runtime config is not baked into the bundle.
      new s3deploy.BucketDeployment(this, 'DeployDashboard', {
        sources: [
          dashboardBundledAsset(path.join(__dirname, '../../dashboard-react')),
          s3deploy.Source.jsonData('config.json', {
            region: this.region,
            userPoolId: userPoolId ?? '',
            userPoolClientId: appClient.userPoolClientId,
            hostedUiDomain: hostedUiDomain ?? '',
            apiBaseUrl: '/api',
            wsUrl: wsUrl ?? '',
          }),
        ],
        destinationBucket: this.siteBucket,
        distribution: this.distribution,
        distributionPaths: ['/*'],
      });

      // The WebSocket authorizer (Compute stack) accepts tokens issued to this
      // client by reading its ID from here at run time (R-05). Not a secret: the
      // same ID is in the public /config.json.
      if (props.dashboardClientIdParameterName) {
        new ssm.StringParameter(this, 'DashboardClientIdParameter', {
          parameterName: props.dashboardClientIdParameterName,
          stringValue: appClient.userPoolClientId,
          description: 'Dashboard app client ID, read by the WebSocket $connect authorizer',
        });
      }

      new cdk.CfnOutput(this, 'UserPoolClientId', {
        value: appClient.userPoolClientId,
        exportName: `${this.stackName}-UserPoolClientId`,
        description: 'Cognito User Pool Client ID for the SPA',
      });
    }

    // ── CfnOutputs ─────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'CloudFrontUrl', {
      value: `https://${this.distribution.distributionDomainName}`,
      exportName: `${this.stackName}-CloudFrontUrl`,
      description: 'CloudFront distribution URL for the dashboard',
    });

    new cdk.CfnOutput(this, 'DistributionId', {
      value: this.distribution.distributionId,
      exportName: `${this.stackName}-DistributionId`,
      description: 'CloudFront distribution ID',
    });

    new cdk.CfnOutput(this, 'SiteBucketName', {
      value: this.siteBucket.bucketName,
      exportName: `${this.stackName}-SiteBucketName`,
      description: 'S3 bucket for dashboard assets',
    });

    // Log retention for the BucketDeployment handler (T-09, R-06).
    retainLambdaLogs(this);
  }
}
