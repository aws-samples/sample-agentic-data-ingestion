# Deploy: Dashboard Hosting + Cognito Auth

End-to-end deployment and verification guide for the hosted dashboard with Cognito authentication.

> **Shortcut:** `./deploy.sh` from the project root performs every step in this
> guide (prerequisite validation, dual-region bootstrap, all-stack deploy, and
> the automatic dashboard build). This document is the manual/step-by-step
> equivalent, useful for targeted redeploys and verification.

## Pre-requisites

- AWS CDK v2 installed (`npm install -g aws-cdk`)
- Node.js 20+ and npm

### Choose the target account

**There is no default account.** Two live accounts run this system, one of which is a
fresh-account validation account, so a pasted account literal deploys somewhere plausible and
wrong. Name the profile and let the account be read back from it. That convention exists
because a hardcoded account number was once copied verbatim into a deploy aimed at the other
account, and updated a Data stack in the wrong place. Listing both accounts here to copy from
was rejected as the same trap with two doors, and a blank `AWS_PROFILE` fails the `sts` call
rather than defaulting, so it fails closed.

Set both variables **before** the bootstrap commands below, which read them:

```bash
cd agentic_data_ingestion/deploy-cdk
export AWS_PROFILE=              # REQUIRED — no default; fill in your target profile
export CDK_DEFAULT_REGION=us-west-2
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text)
echo "deploying to $CDK_DEFAULT_ACCOUNT / $CDK_DEFAULT_REGION"   # verify before continuing
```

Leaving `AWS_PROFILE` blank fails the `sts` call rather than defaulting — it fails closed.
Read the `echo` and confirm the account is the one you meant before going on. This matches
`deploy-cdk/README.md` and `deploy.sh`, which already derive the account this way;
`lib/resolve-env.ts` rejects anything that is not 12 digits.

### Bootstrap both regions

CDK must be bootstrapped in **both** the application region **and `us-east-1`**. The
EdgeWaf stack deploys a CLOUDFRONT-scope WebACL to `us-east-1` and is wired into the Hosting
distribution via cross-region references, so `us-east-1` must be bootstrapped even when you
deploy the app to a different region:

```bash
npx cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION
npx cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/us-east-1
```

## 1. Dashboard Build (automatic)

The dashboard is built automatically during `cdk synth`/`cdk deploy` via CDK
asset bundling — there is no manual `npm run build` step. The Hosting stack
builds from the `dashboard-react/` source (local Node if available, a `node:20`
container otherwise) and deploys the result to S3. The first deploy is slower
because of this; subsequent deploys hit the CDK asset cache unless dashboard
source changed.

## 2. Deploy Stacks

Stacks must deploy in order: **Auth → Compute → EdgeWaf → Hosting**.

### Option A — Deploy individually (recommended for first deploy)

```bash
cd agentic_data_ingestion/deploy-cdk

# 1) Auth stack — Cognito User Pool + Hosted UI domain
npx cdk deploy AgenticDataIngestion-Auth-dev --require-approval never

# 2) Compute stack — REST API + WebSocket API with Cognito authorizers + regional WAF
npx cdk deploy AgenticDataIngestion-Compute-dev --require-approval never

# 3) EdgeWaf stack — CLOUDFRONT-scope WAF (deploys to us-east-1)
npx cdk deploy AgenticDataIngestion-EdgeWaf-dev --require-approval never

# 4) Hosting stack — S3 + CloudFront + Cognito App Client + config.json
npx cdk deploy AgenticDataIngestion-Hosting-dev --require-approval never
```

### Option B — Deploy all at once

CDK resolves the dependency graph automatically:

```bash
cd agentic_data_ingestion/deploy-cdk
npx cdk deploy --all --require-approval never
```

> Note: `--all` deploys every stack. If other stacks (Foundation, Data, etc.) are
> unchanged this is harmless — CDK skips no-op updates.

## 3. Capture Stack Outputs

After deploy, note the following from the CloudFormation outputs:

| Output | Stack | Description |
|--------|-------|-------------|
| `CloudFrontUrl` | AgenticDataIngestion-Hosting-dev | The dashboard URL |
| `UserPoolId` | AgenticDataIngestion-Auth-dev | Cognito User Pool ID |
| `UserPoolClientId` | AgenticDataIngestion-Hosting-dev | App client ID (created in Hosting) |
| `HostedUiDomain` | AgenticDataIngestion-Auth-dev | Cognito Hosted UI domain prefix |

You can retrieve outputs with:

```bash
aws cloudformation describe-stacks --stack-name AgenticDataIngestion-Auth-dev \
  --query "Stacks[0].Outputs" --output table

aws cloudformation describe-stacks --stack-name AgenticDataIngestion-Hosting-dev \
  --query "Stacks[0].Outputs" --output table
```

## 4. Create a Test User

Since self-service sign-up is disabled, create a user via the CLI:

```bash
# Replace <UserPoolId> with the actual value from step 3
USER_POOL_ID=<UserPoolId>

# Generate a random temporary password (pool policy: 16+ chars) — no plaintext
# literal in shell history. The user sets a real password at first Hosted UI
# sign-in; the temporary one expires after 24 hours.
TEMP_PASSWORD=$(aws secretsmanager get-random-password \
  --password-length 24 --require-each-included-type --output text)

aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username testuser@example.com \
  --user-attributes Name=email,Value=testuser@example.com Name=email_verified,Value=true \
  --temporary-password "$TEMP_PASSWORD"

echo "Temporary password: $TEMP_PASSWORD"
```

## 5. Verification Checklist

Open the CloudFront URL in a browser and verify each item:

### Authentication flow

- [ ] **CloudFront URL redirects to login** — Opening the CloudFront URL without a session redirects to the Cognito Hosted UI login page.
- [ ] **First sign-in enrolls MFA** — The first sign-in asks the test user to set a password and then to enroll an authenticator app (TOTP), because MFA is required by default.
- [ ] **Post-login dashboard renders** — After signing in with the test user credentials, the dashboard loads and renders correctly.

### API authorization

- [ ] **Authenticated `/api/pipelines` succeeds** — With a valid session, the dashboard's pipeline list loads (HTTP 200). Verify in browser DevTools Network tab.
- [ ] **WebSocket connects with token** — In browser DevTools, verify the WebSocket connection opens successfully (check `wss://...?token=<jwt>` in the Network/WS tab).

### Security enforcement

- [ ] **Unauthenticated `/api` call → 401** — From a terminal, call the CloudFront `/api/pipelines` endpoint without an Authorization header:
  ```bash
  curl -s -o /dev/null -w "%{http_code}" https://<CloudFrontDomain>/api/pipelines
  # Expected: 401
  ```
- [ ] **Direct execute-api call → 403 (WAF blocks)** — Call the raw API Gateway URL directly (bypassing CloudFront). The regional WAF blocks requests missing the `X-Origin-Verify` header:
  ```bash
  # Get the raw API endpoint from Compute stack outputs
  curl -s -o /dev/null -w "%{http_code}" https://<api-id>.execute-api.<region>.amazonaws.com/v1/api/pipelines
  # Expected: 403
  ```

## SPA routing and the 403 residual

The dashboard is a single-page app served from S3, so a deep link such as
`/pipelines/abc-1234/code` has no object behind it. What makes those links
resolve is a **CloudFront Function on viewer-request**, not `errorResponses` on
the distribution.

### The mechanism

`deploy-cdk/lib/spa-router-function.ts` holds both halves: the deployed function
source (`SPA_ROUTER_FUNCTION_CODE`, `:89`) and the construct that carries it
(`SpaRouterFunction`, `:130`). The Hosting stack instantiates it as `SpaRouter`
(`hosting-stack.ts:273`) and associates it with the **default (S3) behaviour
only**, on `VIEWER_REQUEST` (`hosting-stack.ts:281-286`). The function rewrites a
request to `/index.html` unless the **final** path segment looks like a filename —
a dot followed by 1–8 alphanumerics beginning with an ASCII letter — in which case
it passes through to S3 untouched. Final segment only, so
`/pipelines/orders.v2/code` is still a route.

**The distribution carries no `errorResponses`** (`hosting-stack.ts:294-297`). It
used to: 403 and 404 were mapped to HTTP 200 with the body of `/index.html`.
CloudFront custom error responses are a property of `DistributionConfig` and
cannot be scoped to a behaviour, so that mapping also rewrote every error the
`/api/*` behaviour produced — a real `404 no test results yet`
(`handlers/pipelines_artifacts.py:91`) arrived as HTML under a 200, which
`api-client._unwrap` could only report as
`ApiError(200, 'Response body could not be read as JSON')`
(`hosting-stack.ts:254-263`). A function association **is** per-behaviour, and
that is the whole reason it is the mechanism: it is the only way to give the SPA
its fallback without also rewriting `/api/*`. Lambda@Edge was rejected as the
alternative, because it needs an execution role and a us-east-1 replica, adding
IAM surface and cdk-nag findings for logic that is a dozen lines of string work.

Two do-NOTs live at `hosting-stack.ts:265-272`, pinned by
`deploy-cdk/test/p56-spa-router-function.test.ts`:

- **Do not reintroduce `errorResponses`** on this distribution.
- **Do not attach this function to `additionalBehaviors['/api/*']`** — it would
  rewrite API request URIs to `/index.html` before they reached API Gateway,
  which is a worse version of the same defect.

### The accepted residual

**A genuinely missing S3 object now returns S3's own 403 instead of `index.html`
under a 200. That is accepted, not a defect** — see §*Behaviour change from
dropping `errorResponses`* at `spa-router-function.ts:68-74`. A missing asset
must not masquerade as a working app; the cost of that honesty is that the
rewrite has to cover every legitimate SPA route, which is what the route table
in the test file exists to hold.

The heuristic leaves one narrower residual, also accepted: a final path segment
whose dot-suffix *does* begin with a letter (`/pipelines/orders.v2`) is read as a
filename, reaches S3 and answers 403 (§*The extensionless heuristic, and its one
residual*, `spa-router-function.ts:57-66`). Accepted on evidence rather than
optimism — a derived pipeline id cannot contain a dot, because
`handlers/pipelines_crud.py:1894-1896` filters the name with
`c.isalnum() or c == "-"` before appending 8 hex characters of a uuid4. So the
case is unreachable through the product's own id generation.

**Hardening the client to detect an HTML body on an API path was considered and
explicitly rejected.** This is the workaround a reader hitting an ambiguous 403
will otherwise invent, so the rejection is stated rather than implied. The
reason: *by then the true status is gone*, so the best such a check could do is
guess 404, and it would leave the 403 case, meaning every WAF block and every
Cognito authorizer rejection, still ambiguous. Diagnose a 403 at its source
(CloudFront and WAF logs, the authorizer's CloudWatch logs) rather than inferring
it from a response body. That rejection and the `ApiError(200, …)` symptom above
are two faces of one decision. Revisit it only for a *legitimate* route whose
final segment carries a letter-prefixed dot suffix, at which point the fix is to
narrow the extension test to an allowlist, **not** to restore `errorResponses`.

### The 128-character `Comment` cap

CloudFront caps `FunctionConfig.Comment` at **128 characters**, and **nothing
between the TypeScript and the API call enforces it**. `cdk synth` renders an
over-length comment happily and every other suite stays green; the rejection
arrives from the CloudFront API during CREATE:

```
CREATE_FAILED | AWS::CloudFront::Function | SpaRouterFunction95E96187
"Invalid request provided: AWS::CloudFront::Function: The parameter Comment is
 too big. (Service: CloudFront, Status Code: 400)"
```

That is what happened to `AgenticDataIngestion-Hosting-dev` on 2026-08-31, at 136
characters — 8 over. The stack rolled back cleanly, so the cost was a wasted
deploy cycle rather than damage.

**A synth-time assertion now pins the bound.**
`deploy-cdk/test/p56-spa-router-function.test.ts` reads
`FunctionConfig.Comment` off the **synthesized template** and asserts its length
against `CLOUDFRONT_FUNCTION_COMMENT_MAX_CHARS = 128` (`:106`; the assertion is
at `:316-350`). Off the rendered template rather than the TypeScript source,
because what fails is what CloudFormation submits. The same block bounds `Name`
(64 characters, and updates to it require replacement) and `FunctionCode`
(10,000 UTF-8 bytes) for the same reason — three deploy-time rejections synth
does not see.

The comment at `spa-router-function.ts:151-152` is a console **label**, not the
reasoning; the argument lives in the module docstring, which has no length limit
and is where a reader actually goes. So the fix for an over-length comment is to
shorten the comment: 128 is a documented service limit, not a local preference,
and raising the constant does not raise the service's bound.

**Pinning the exact comment literal was rejected**, and the reason is worth
carrying: it makes every rewording a test failure, and it teaches the next reader
to update the literal without reading why the bound exists. The test asserts the
*bound*, so a reworded comment stays green while an over-length one fails. The
limit itself was read from the two AWS references rather than inferred from the
observed failure, because the error message does not state the number.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| CloudFront returns 403 on `/` | S3 bucket or OAC misconfigured | Check bucket policy grants CloudFront OAC read |
| Login redirects fail (invalid_redirect) | Callback URL mismatch | Verify app client callback URLs include the CloudFront domain |
| `/api` returns 401 with a valid login | Sending the **access token** | The scope-less API Gateway Cognito authorizer expects the **ID token**, not the access token — send `id_token` as the `Authorization` value |
| `/api` returns 401 with the ID token | `Bearer ` prefix on the header | Send the **raw** JWT as the `Authorization` value with **no `Bearer ` prefix** |
| WebSocket fails to connect | JWT expired; token issued to an app client that is not allowed; or no allowed client found yet (Hosting not deployed, so its client-ID parameter does not exist) | Check the WS authorizer Lambda logs in CloudWatch: an empty allowed list logs `Configuration error` |
| Direct API call returns 200 (not 403) | WAF not associated with API stage | Verify the regional WebACL is associated in the AWS console |

## Tearing Down

To remove the hosting infrastructure:

```bash
cd agentic_data_ingestion/deploy-cdk
npx cdk destroy AgenticDataIngestion-Hosting-dev
npx cdk destroy AgenticDataIngestion-EdgeWaf-dev
npx cdk destroy AgenticDataIngestion-Compute-dev
npx cdk destroy AgenticDataIngestion-Auth-dev
```

> Destroy in reverse order to avoid dependency errors. The S3 bucket must be empty
> before the stack can be deleted; CDK handles that automatically —
> `hosting-stack.ts:103` sets `removalPolicy: DESTROY` + `autoDeleteObjects`
> unconditionally, for every environment. A stack left in `ROLLBACK_COMPLETE`
> after a failed create must be deleted before it can be re-created.
>
> These four stacks destroy cleanly. Removing the **whole** deployment is a
> different job — the Data stack has termination protection and retained DynamoDB
> tables. See [QUICKSTART.md](../QUICKSTART.md#teardown).
