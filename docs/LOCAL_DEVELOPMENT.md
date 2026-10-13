# Local Development (Dashboard + Cognito Auth)

After the IAM → Cognito authorization switch, the old `localhost:8080` SigV4 signing
proxy no longer works. This document explains how to run the React dashboard locally
against the deployed Cognito-protected API.

---

## Prerequisites

- The CDK stacks are deployed (`cdk deploy --all`).
- You have the CloudFront distribution URL from the stack outputs.
- An admin has created your user in the Cognito User Pool.

---

## 1. Configure environment variables

Create `dashboard-react/.env.local` (git-ignored) with the values from your deployment:

```bash
# Required — Cognito / auth config (from CDK stack outputs or config.json)
VITE_REGION=us-west-2
VITE_USER_POOL_ID=us-west-2_XXXXXXXXX
VITE_USER_POOL_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxx
VITE_HOSTED_UI_DOMAIN=agentic-data-ingestion-<account-id>-dev.auth.us-west-2.amazoncognito.com
VITE_WS_URL=wss://xxxxxxxxxx.execute-api.us-west-2.amazonaws.com/prod

# Proxy target — MUST be the CloudFront URL (see note below)
VITE_API_PROXY_TARGET=https://d1234abcdef8.cloudfront.net
```

You can find these values in:
- The deployed `config.json` on the S3 bucket (or the CloudFront `/config.json` URL).
- CDK stack outputs after deployment.

---

## 2. Start the dev server

```bash
cd dashboard-react
npm run dev
```

The Vite dev server starts on `http://localhost:5173`. The `/api` proxy is configured
in `vite.config.ts` to forward requests to your `VITE_API_PROXY_TARGET`.

---

## 3. Authenticate (get a dev token)

The app uses `react-oidc-context` for authentication. When you open
`http://localhost:5173`, the auth library will detect you have no session and redirect
you to the Cognito Hosted UI.

1. Open `http://localhost:5173` in your browser.
2. You'll be redirected to the Cognito Hosted UI login page.
3. Sign in with your admin-created credentials.
4. The Hosted UI redirects back to `http://localhost:5173` with an authorization code.
5. The OIDC library exchanges the code for tokens (PKCE flow) and stores them.
6. The app renders normally; API calls include the token automatically.

> **Note:** For the callback to work, `http://localhost:5173` must be registered as a
> callback URL in the Cognito app client. It is not by default. Deploy the Hosting stack
> with the local-development flag, which adds the callback and logout URLs and the
> `/api/*` CORS origin together:
>
> ```bash
> npx cdk deploy AgenticDataIngestion-Hosting-dev -c adi:allowLocalhost=true --exclusively
> ```
>
> Pass the same flag on every later deploy of that stack, or the next deploy removes
> the URLs again. Do not add them by hand in the Cognito console: CloudFormation
> resets them on the next deploy. Leave the flag off for any deployment that holds
> real data.

---

## 4. Why CloudFront, not the raw API Gateway URL?

The deployment uses an `X-Origin-Verify` secret header for defense-in-depth:

- CloudFront injects the header on every `/api/*` request to the API Gateway origin.
- A regional WAF on the API Gateway **blocks** any request missing this header.
- This means direct calls to `https://xxxxxxxxxx.execute-api.us-west-2.amazonaws.com`
  will return `403 Forbidden`.

> **Note:** This regional WAF (on the API stage, in the Compute stack) is separate from
> the CLOUDFRONT-scope WAF that fronts the distribution — that WebACL lives in the
> `AgenticDataIngestion-EdgeWaf-dev` stack in **us-east-1** and is attached to CloudFront via a
> cross-region reference. Neither changes local dev beyond requiring you to proxy
> through CloudFront.

**Therefore, the Vite proxy must target the CloudFront URL**, not the raw execute-api
URL. CloudFront is the only usable entry point to the API.

### Alternative: skip the Vite proxy entirely

Instead of proxying through Vite, you can point the app directly at CloudFront:

```bash
# In .env.local
VITE_API_BASE_URL=https://d1234abcdef8.cloudfront.net/api
```

The `config.ts` loader uses `VITE_API_BASE_URL` as the API base. With this set, the
`ApiClient` calls CloudFront directly, which is cross-origin from `localhost:5173`. It
works only with `-c adi:allowLocalhost=true`: without the flag CloudFront strips
`Access-Control-Allow-Origin` from every `/api/*` response, so the browser blocks the
read. The proxy approach is recommended because it is same-origin and needs no CORS at
all (it still needs the flag for the Cognito callback).

---

## 5. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `403` on `/api/*` calls | Proxy targeting raw execute-api URL | Set `VITE_API_PROXY_TARGET` to the CloudFront URL |
| `401 Unauthorized` | Token expired or missing | Sign out and sign back in via Hosted UI |
| Redirect loop on login | `localhost:5173` not in Cognito callback URLs | Redeploy Hosting with `-c adi:allowLocalhost=true` |
| `config.json` 404 | Env vars not set and no `public/config.json` | Create `.env.local` with all `VITE_*` vars |
| WebSocket connection fails | Token not attached or WS URL wrong | Check `VITE_WS_URL` and ensure token is passed as `?token=` query param |

---

## 6. Summary of config resolution

The `src/config.ts` module resolves configuration in this order:

1. **Vite env vars** (`VITE_*` in `.env.local`) — used in local dev, no network request.
2. **`/config.json`** — fetched at runtime in production (deployed by CDK to S3).

For local dev, option 1 is simplest: just set all vars in `.env.local`.
