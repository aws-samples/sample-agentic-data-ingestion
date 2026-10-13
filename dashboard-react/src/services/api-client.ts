// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * API client with in-flight request deduplication and Lambda proxy unwrapping.
 *
 * Deduplication applies to GET requests ONLY. When multiple callers issue the
 * same GET (same URL) before the first resolves, a single fetch is made and the
 * response is shared with all callers; the cache entry is removed on completion
 * (success or failure) so subsequent requests proceed normally. Mutations
 * (POST/PUT/DELETE) are NEVER deduplicated — two POSTs to the same URL with
 * different bodies must both be dispatched, so they bypass the in-flight map
 * entirely.
 *
 * Auth expiry: a 401 response means the Cognito session is no longer valid (the
 * API Gateway Cognito authorizer rejects expired/invalid tokens with a real HTTP
 * 401 before the request reaches Lambda). On the first 401 the client triggers a
 * single `signinRedirect` via the registered unauthorized handler and guards
 * against re-triggering (`isRedirecting`), so a session that expires mid-session
 * re-logs the user in once instead of surfacing a toast per failed request.
 *
 * Error detail: `ApiError.message` composes `body.error`/`body.message` with
 * `body.details` (the AWS error string `handle_error` puts there), redacted. Before
 * that, a handler's fixed sentence was all a user saw while the cause sat unread in
 * the same body — "Failed to create discovery crawler" with no hint of which AWS
 * call was refused. See `redactErrorDetail` for what is stripped and why.
 *
 * Error class: callers discriminate a failure with `err instanceof ApiError` and
 * then `err.status`, so a response body the client could not read as JSON is
 * raised as an `ApiError` carrying that response's own status rather than
 * escaping as a `SyntaxError` no caller can catch (p54 R3). Two things still
 * reject with something else and are out of R3's scope: a network-level fetch
 * failure from `requestRaw`, and an envelope whose inner `body` STRING is not
 * valid JSON (`JSON.parse` at the wrapper branch below).
 */

/**
 * Credential-shaped substrings, stripped from `body.details` before it reaches
 * a toast or an Alert.
 *
 * `details` carries `str(exception)` from any of `handle_error`'s call sites
 * (`shared/response_utils.py:94`), so promoting it to a user-visible surface
 * widens what an operator can see — and would widen what a credential can
 * escape through if it went unfiltered. The backend redacts the same three
 * shapes at the crawler call sites it owns; this is the client-side half,
 * covering every *other* producer of the field, none of which was written with
 * an operator-visible `details` in mind.
 *
 * The invariant being protected is that JDBC credentials are only ever
 * referenced by Secrets Manager ARN, never as plaintext user/password.
 */
const DETAIL_REDACTIONS: ReadonlyArray<readonly [RegExp, string]> = [
  // Trailing name segment of a Secrets Manager ARN. Region and account stay:
  // "which region" is a live diagnostic for a wrong-region Glue connection.
  [/(:secret:)[^\s"',;)\]}]+/g, '$1***'],
  // Glue ConnectionProperties credential keys. botocore's ParamValidationError
  // formats request parameters into its message, so a malformed
  // ConnectionProperties dict is the shape that could carry a password here.
  [
    /\b(PASSWORD|USERNAME|SECRET_ID|KAFKA_SASL_PLAIN_PASSWORD)\b(["']?\s*[:=]\s*)(["']?)[^\s"',;)\]}]*/gi,
    '$1$2$3***',
  ],
  // SigV4 query parameters — a presigned URL in an error message is a usable
  // credential until these are gone.
  [/\b(X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token)=[^\s&"']*/gi, '$1=***'],
];

/** Apply {@link DETAIL_REDACTIONS} to an error detail string. */
export function redactErrorDetail(detail: string): string {
  return DETAIL_REDACTIONS.reduce(
    (text, [pattern, replacement]) => text.replace(pattern, replacement),
    detail,
  );
}

export class ApiError extends Error {
  public readonly status: number;
  public readonly body: unknown;

  constructor(status: number, body: unknown) {
    // Backend error responses use one of two shapes depending on the
    // handler: `{"error": "..."}` (most dashboard handlers, via
    // shared/response_utils.create_response) or `{"message": "..."}`.
    // Prefer whichever is present so the surfaced toast shows the real
    // server reason instead of a generic "API error 400". Falls back to
    // the status-only string when neither key exists.
    const b =
      typeof body === 'object' && body !== null
        ? (body as Record<string, unknown>)
        : undefined;
    const detail =
      b && typeof b.message === 'string'
        ? b.message
        : b && typeof b.error === 'string'
          ? b.error
          : undefined;
    // `handle_error` returns `{error, details}` where `error` is a fixed
    // sentence chosen by the handler and `details` is the actual AWS error.
    // Reading only `error` is why "Failed to create discovery crawler" was all a
    // tester could see while the cause — the AWS code and message that names it
    // — sat unread in the same response body. Appended rather than substituted:
    // `error` is the sentence the handler wrote for a human, and every caller
    // that reads `body.error` directly (`utils/schema-delete-failure.ts`) keeps
    // getting it verbatim.
    const extra =
      b && typeof b.details === 'string' && b.details.trim() && b.details !== detail
        ? redactErrorDetail(b.details.trim())
        : undefined;
    const composed = detail
      ? extra
        ? `${detail} (${extra})`
        : detail
      : (extra ?? `API error ${status}`);
    super(composed);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

export class ApiClient {
  private inflight = new Map<string, Promise<Response>>();
  private baseUrl: string;
  private getToken: (() => string | null) | null = null;
  private onUnauthorized: (() => void) | null = null;
  /** One-shot guard so many concurrent 401s trigger exactly one redirect. */
  private isRedirecting = false;

  constructor(baseUrl = '/api') {
    this.baseUrl = baseUrl;
  }

  /**
   * Set a callback that returns the current auth token (or null).
   * The auth layer calls this once at startup to wire in its token accessor.
   */
  setTokenAccessor(fn: () => string | null): void {
    this.getToken = fn;
  }

  /**
   * Register the handler invoked (once) on the first 401 response — typically
   * `react-oidc-context`'s `signinRedirect`. The auth layer wires this at
   * startup alongside the token accessor.
   */
  setUnauthorizedHandler(fn: () => void): void {
    this.onUnauthorized = fn;
  }

  /**
   * Trigger the unauthorized handler at most once. Subsequent 401s (from other
   * in-flight requests during the same session expiry) are ignored so the user
   * sees a single re-login redirect rather than a redirect/toast per request.
   */
  private handleUnauthorized(): void {
    if (this.isRedirecting) return;
    this.isRedirecting = true;
    this.onUnauthorized?.();
  }

  /**
   * Low-level fetch with in-flight deduplication. Returns raw Response.
   * Use for file uploads or streaming where you need the Response object.
   */
  async requestRaw(url: string, options: RequestInit = {}): Promise<Response> {
    const method = (options.method || 'GET').toUpperCase();
    const fullUrl = `${this.baseUrl}${url}`;
    const key = `${method}:${fullUrl}`;

    // Deduplicate GET requests only. Mutations (POST/PUT/DELETE) must always
    // dispatch — two POSTs to the same URL with different bodies are distinct
    // operations and collapsing them would drop the second body's effect.
    const dedupable = method === 'GET';

    if (dedupable) {
      const existing = this.inflight.get(key);
      if (existing) {
        return existing;
      }
    }

    // Inject Authorization header if a token accessor is set and returns a token.
    // The API Gateway Cognito User Pools authorizer validates the raw JWT taken
    // directly from the Authorization header value against the User Pool's JWKS,
    // so it requires NO `Bearer ` scheme prefix (unlike a custom/OAuth bearer
    // scheme). Sending `Bearer <jwt>` makes the header value unparseable as a JWT
    // and the authorizer rejects the request with 401 before it reaches Lambda.
    // The token here is the Cognito access token supplied by the auth provider.
    const token = this.getToken?.() ?? null;
    let headers: HeadersInit = options.headers ?? {};
    if (token) {
      headers = { ...headers, Authorization: token };
    }

    const promise = fetch(fullUrl, { ...options, method, headers }).then(
      (response) => {
        if (dedupable) this.inflight.delete(key);
        // A real HTTP 401 (from the API Gateway Cognito authorizer) means the
        // session expired — trigger a single re-login redirect. Lambda-proxy
        // wrapped 401s (HTTP 200 with { statusCode: 401 }) are handled in
        // _unwrap where the wrapper is parsed.
        if (response.status === 401) this.handleUnauthorized();
        return response;
      },
      (error) => {
        if (dedupable) this.inflight.delete(key);
        throw error;
      },
    );

    if (dedupable) {
      this.inflight.set(key, promise);
    }
    return promise;
  }

  /**
   * Unwrap Lambda proxy response format and parse JSON.
   * Handles: { statusCode, body: string|object } and plain JSON.
   * Throws ApiError on statusCode >= 400, on a non-ok status, and on a body
   * that could not be read as JSON.
   */
  private async _unwrap<T>(url: string, options: RequestInit = {}): Promise<T> {
    const res = await this.requestRaw(url, options);

    // The body read is inside a `try` and the STATUS is the classifier (p54 R3).
    // A body that is not JSON — a CloudFront or WAF HTML error page, an empty
    // body, a truncated response — makes `.json()` throw `SyntaxError`, and a
    // `SyntaxError` is not an `ApiError`, so it slips past every caller's
    // `err instanceof ApiError` discrimination: `use-code.ts:56` cannot see that
    // a 404 was a 404, so `isError` goes true and `TestResultsViewer` reports
    // "Failed to load test results." for what is really a transport or edge
    // failure.
    let raw;
    try {
      // Clone before reading body — dedup may share the same Response object
      raw = await res.clone().json();
    } catch {
      // `ApiError` is REUSED in both failure arms, deliberately: every caller
      // that discriminates a failure does it with `err instanceof ApiError`, and
      // `_unwrap` is reached from 83 call sites across 36 files, 30 of them
      // hooks. A new class would be invisible to all of them (R3.4).
      if (!res.ok) {
        // R3.1 + R3.2: the response already said what went wrong, so classify
        // by that status exactly as a parseable error body would be. The
        // unreadable body is incidental and is named so the operator can tell
        // this from a handler that answered with a reason.
        throw new ApiError(res.status, {
          error: `API error ${res.status} (response body could not be read as JSON)`,
        });
      }
      // R3.3: a status below 400 carrying an unreadable body is NOT an HTTP
      // error, so the sentence is what makes the object honest about which of
      // the two it is. `ApiError`'s constructor lifts `body.error` into
      // `message` (`:35-41` above), so it reaches a toast as written.
      throw new ApiError(res.status, {
        error: `Response body could not be read as JSON (HTTP ${res.status})`,
      });
    }

    // ── Parse succeeded: unchanged from here down (R3.4) ────────────────────
    // Detect Lambda proxy wrapper
    if (raw != null && typeof raw === 'object' && 'statusCode' in raw) {
      const body =
        typeof raw.body === 'string' ? JSON.parse(raw.body) : raw.body;
      // A wrapped 401 (HTTP 200 envelope carrying statusCode 401) also means
      // the session is no longer authorized — trigger the one-shot redirect.
      if (raw.statusCode === 401) this.handleUnauthorized();
      if (raw.statusCode >= 400) throw new ApiError(raw.statusCode, body);
      return body as T;
    }

    // Plain JSON (non-proxy response)
    if (!res.ok) throw new ApiError(res.status, raw);
    return raw as T;
  }

  async get<T>(url: string): Promise<T> {
    return this._unwrap<T>(url);
  }

  async post<T>(url: string, body: unknown): Promise<T> {
    return this._unwrap<T>(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  async put<T>(url: string, body: unknown): Promise<T> {
    return this._unwrap<T>(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  async delete<T>(url: string): Promise<T> {
    return this._unwrap<T>(url, { method: 'DELETE' });
  }

  /** Visible for testing — number of in-flight entries. */
  get inflightSize(): number {
    return this.inflight.size;
  }
}

export const apiClient = new ApiClient();
