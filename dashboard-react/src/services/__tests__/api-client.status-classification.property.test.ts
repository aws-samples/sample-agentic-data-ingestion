// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p54 task 1.4 — Property 3, generatively over status x body shape.
 *
 * **Validates: Requirements 3.1, 3.2, 3.3, 3.4**
 *
 * > **Property 3: A raised API error always carries the response's own status.**
 * > *For any* HTTP status and any body — parseable JSON, non-JSON text, or empty
 * > — the error `_unwrap` raises carries that response's status, and the one-shot
 * > unauthorized handler is invoked **at most once** per response regardless of
 * > which of the two 401 shapes arrived.
 *
 * **Why generative rather than a sixth set of examples.** `_unwrap` is a
 * chokepoint: it is reached from **83 non-test call sites across 36 files, 30 of
 * them hooks** (`apiClient.get` alone is **50 sites in 25 hook files**), and
 * **19** non-test sites discriminate a failure with `err instanceof ApiError`
 * before reading `err.status` — that last figure is the one R3.4 is about, because
 * a status the client invented, or an error of some other class, is invisible to
 * all 19. Task 0.1's characterization pins the five parseable-body shapes it
 * enumerates and task 1.1's pins the two unparseable arms by named example; what
 * neither does is cross the status axis with the body axis. This file does, so a
 * status the client substitutes for the response's own fails here rather than in a
 * hook.
 *
 * Three things this file deliberately sweeps that the example suites do not:
 *
 * - **the whole 200-599 status range**, not four hand-picked codes — which is how
 *   the 3xx band gets covered at all, since `res.ok` is 200-299 only;
 * - **the two 401 trigger sites together**. There are exactly two:
 *   `requestRaw` (`api-client.ts:137`, on the real HTTP status) and `_unwrap`
 *   (`:203`, on a wrapped `statusCode`). `isRedirecting` is what collapses them
 *   for one response, so the handler count is asserted as an exact expected
 *   number over every status x shape pair rather than for three named ones;
 *   (**line-reference correction**: the two sites were at `:129` and `:160`
 *   before task 1.1 added 46 lines to this file, and 0.1's and 1.1's docblocks
 *   still cite the old pair, as does 1.4's brief. `_unwrap` itself is at `:158`,
 *   not 0.1's `:149`. Verified by grep at the time of writing; those files are
 *   not edited here because they must pass unmodified.)
 * - **the envelope/HTTP disagreement in BOTH directions.** 0.1 covers a 500
 *   envelope over an HTTP 502; the generator also reaches an envelope declaring
 *   `< 400` over a failing HTTP status, which resolves. That is R3.4's
 *   "parse succeeded -> unchanged" arm, asserted rather than assumed.
 *
 * **Expectations derive from R3.1-R3.4, not from reading `_unwrap`.** The oracle
 * below is written from the four criteria and each of its branches cites the one
 * it implements. A second, oracle-independent assertion rides alongside it: the
 * status carried must be one the *response* declared (its HTTP status, or the
 * `statusCode` its own envelope carried), so even a wrong oracle cannot let an
 * invented status through.
 *
 * **No ledger record governs `api-client.ts`.** `why` on it returns five commits,
 * every one with `records: []`, three of them the documented broad snapshots
 * (`0176ccc686`, `7ad6ca1ad9`, `2d64f7a7a4`); `decisions(concept="api")`,
 * `"error-handling"`, `"auth"` and `"testing"` return nothing about `_unwrap`,
 * `ApiError` or error classification, and `decisions(scope=".../src/services")`
 * returns only BROAD `99050e6` records plus rulings on other files. That is a
 * **gap in the ledger, not an all-clear** — this file is constrained by R3 and by
 * the two example suites, and by nothing else.
 *
 * fast-check `^3.15.0` (an existing dependency), `{ numRuns: 100 }` per the
 * in-tree convention.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import * as fc from 'fast-check';
import { ApiClient, ApiError } from '../api-client';

// ---------------------------------------------------------------------------
// The input space
// ---------------------------------------------------------------------------

/**
 * Statuses that MUST carry a null body. `new Response('x', { status: 204 })`
 * throws, so these appear only against the empty-body shape — which is the only
 * shape they can legally take anyway, so nothing is lost by the split.
 */
const NULL_BODY_STATUSES = [204, 205, 304];

/**
 * `Response`'s constructor accepts 200-599 and throws `RangeError` outside it,
 * so that is the representable range rather than a choice.
 *
 * Weighted rather than uniform: a bare `fc.integer({ min: 200, max: 599 })` hits
 * 401 about once in 400 draws, and 401 is the status half of this property is
 * about. The named codes carry the boundaries that matter (`ok`/not-ok at 300,
 * the R3.2 threshold at 400) alongside the wide draw that finds the rest.
 */
const NAMED_STATUSES = [200, 201, 204, 299, 300, 302, 304, 399, 400, 401, 403, 404, 500, 502, 503] as const;

const anyStatus: fc.Arbitrary<number> = fc.oneof(
  { weight: 3, arbitrary: fc.constantFrom(...NAMED_STATUSES) },
  { weight: 1, arbitrary: fc.integer({ min: 200, max: 599 }) },
);

const statusThatCanCarryABody: fc.Arbitrary<number> = anyStatus.filter(
  (s) => !NULL_BODY_STATUSES.includes(s),
);

/**
 * A JSON value that is NOT a Lambda proxy envelope. The key set deliberately
 * excludes `statusCode`: envelope detection is `'statusCode' in raw`, so a plain
 * body carrying that key would be an envelope and would belong to the other
 * generator. Non-object values are included because `raw != null && typeof raw
 * === 'object'` is the detection guard, so a bare number, string, boolean or
 * `null` reaches the plain arm and several hooks receive exactly those.
 */
const plainJsonValue: fc.Arbitrary<unknown> = fc.oneof(
  fc.record({ items: fc.array(fc.string(), { maxLength: 3 }), total: fc.nat({ max: 99 }) }),
  fc.record({ message: fc.string({ minLength: 1, maxLength: 20 }) }),
  fc.record({ error: fc.string({ minLength: 1, maxLength: 20 }) }),
  fc.array(fc.record({ id: fc.string({ minLength: 1, maxLength: 6 }) }), { maxLength: 3 }),
  fc.string({ maxLength: 10 }),
  fc.integer(),
  fc.boolean(),
  fc.constant(null),
);

/** Text that `JSON.parse` cannot read — an edge error page, a WAF block page, a truncated read. */
const nonJsonText: fc.Arbitrary<string> = fc.oneof(
  fc.constant('<html><body><h1>502 Bad Gateway</h1></body></html>'),
  fc.constant('<!DOCTYPE html><html><head><title>ERROR</title></head></html>'),
  fc.constant('Request blocked.'),
  fc.constant('{"truncated": '),
  fc.constant('   '),
  fc.stringMatching(/^[<a-zA-Z ]{1,24}$/),
);

type BodyShape =
  | { kind: 'plainJson'; value: unknown }
  | { kind: 'envelope'; statusCode: number; inner: unknown; asString: boolean }
  | { kind: 'nonJsonText'; text: string }
  | { kind: 'empty' };

/**
 * The envelope's inner body, kept clear of the documented residual.
 *
 * `_unwrap`'s wrapper branch is `typeof raw.body === 'string' ? JSON.parse(raw.body)
 * : raw.body`, and that `JSON.parse` sits **inside** the parse-succeeded arm R3.4
 * pins as unchanged — so an envelope whose inner `body` is a string that is not
 * itself valid JSON escapes as `SyntaxError`. Task 1.1 left that residual there
 * deliberately (`api-client.ts:23-25` names it) and task 1.4 may not change
 * `api-client.ts`, so generating the shape into a "must be `ApiError`" assertion
 * would fail for a reason this task is not allowed to fix.
 *
 * Keeping clear of it takes a filter, not just a `JSON.stringify`, and **this
 * generator's first draft got it wrong** — the counterexample is recorded in the
 * residual block at the foot of this file. A bare string carried through
 * unserialised (`asString: false`, `inner: ''`) IS a string at the wrapper branch,
 * so it reaches `JSON.parse('')`. Hence:
 *
 * - `asString: true` -> `JSON.stringify(inner)`, which is valid JSON for every
 *   value including a string, and is what the Lambda proxy actually emits;
 * - `asString: false` -> a **non-string** value only, which the branch passes
 *   through untouched.
 */
const envelopeShape: fc.Arbitrary<BodyShape> = fc.oneof(
  fc
    .tuple(anyStatus, plainJsonValue)
    .map(([statusCode, inner]) => ({
      kind: 'envelope' as const,
      statusCode,
      inner,
      asString: true,
    })),
  fc
    .tuple(
      anyStatus,
      plainJsonValue.filter((v) => typeof v !== 'string'),
    )
    .map(([statusCode, inner]) => ({
      kind: 'envelope' as const,
      statusCode,
      inner,
      asString: false,
    })),
);

interface ResponseSpec {
  status: number;
  shape: BodyShape;
  /** Which wrapper reaches `_unwrap`. All four delegate to it, so the method is a dimension. */
  method: 'get' | 'post' | 'put' | 'delete';
  path: string;
}

const method: fc.Arbitrary<ResponseSpec['method']> = fc.constantFrom(
  'get',
  'post',
  'put',
  'delete',
);

const path: fc.Arbitrary<string> = fc.stringMatching(/^\/[a-z]{1,12}$/);

/** Every shape that can accompany a body. Named so the 401 generators can reuse it. */
const bodyShapeWithABody: fc.Arbitrary<BodyShape> = fc.oneof(
  plainJsonValue.map((value) => ({ kind: 'plainJson' as const, value })),
  envelopeShape,
  nonJsonText.map((text) => ({ kind: 'nonJsonText' as const, text })),
);

/** Any shape at all, paired with a status it can legally take. */
const statusAndShape: fc.Arbitrary<{ status: number; shape: BodyShape }> = fc.oneof(
  // The empty-body shape is the only one the null-body statuses can take, so it
  // draws from the full status range.
  anyStatus.map((status) => ({ status, shape: { kind: 'empty' } as BodyShape })),
  fc
    .tuple(statusThatCanCarryABody, bodyShapeWithABody)
    .map(([status, shape]) => ({ status, shape })),
);

const withMethodAndPath = (
  base: fc.Arbitrary<{ status: number; shape: BodyShape }>,
): fc.Arbitrary<ResponseSpec> =>
  fc
    .tuple(base, method, path)
    .map(([{ status, shape }, m, p]) => ({ status, shape, method: m, path: p }));

const responseSpec: fc.Arbitrary<ResponseSpec> = withMethodAndPath(statusAndShape);

/** An envelope that declares `statusCode: 401` — `_unwrap`'s trigger site (`:203`). */
const envelope401: fc.Arbitrary<BodyShape> = fc
  .tuple(
    plainJsonValue,
    plainJsonValue.filter((v) => typeof v !== 'string'),
    fc.boolean(),
  )
  .map(([stringInner, objectInner, asString]) => ({
    kind: 'envelope' as const,
    statusCode: 401,
    inner: asString ? stringInner : objectInner,
    asString,
  }));

/** Any shape that is NOT a wrapped 401, so case (a) below isolates one trigger site. */
const shapeWithoutWrapped401: fc.Arbitrary<BodyShape> = bodyShapeWithABody.filter(
  (s) => !(s.kind === 'envelope' && s.statusCode === 401),
);

/**
 * The three ways a response can be unauthorized, constructed rather than filtered
 * for out of the general draw.
 *
 * At 100 runs the general generator lands a 401 about 7 times — enough to be
 * likely, not enough to be *guaranteed*, and R3.4's one-shot clause is exactly the
 * half of this property that a run with no 401 in it would assert nothing about.
 * These three cases are the two trigger sites separately and then together, which
 * is the "for either 401 shape" the requirement asks for made structural.
 */
const unauthorizedSpec: fc.Arbitrary<ResponseSpec> = withMethodAndPath(
  fc.oneof(
    // (a) a real HTTP 401 only — `requestRaw` (`:137`) is the sole trigger site.
    shapeWithoutWrapped401.map((shape) => ({ status: 401, shape })),
    // (b) a wrapped `statusCode: 401` only — `_unwrap` (`:203`) is the sole one.
    fc
      .tuple(
        statusThatCanCarryABody.filter((s) => s !== 401),
        envelope401,
      )
      .map(([status, shape]) => ({ status, shape })),
    // (c) BOTH at once, for one response. This is the case that makes "at most
    // once per response" a real assertion rather than a restatement of the
    // cross-request guard: both sites fire and `isRedirecting` collapses them.
    envelope401.map((shape) => ({ status: 401, shape })),
  ),
);

// ---------------------------------------------------------------------------
// Building the response, and the oracle
// ---------------------------------------------------------------------------

/** A real `Response`, so `clone()` and `json()` behave as they do in the browser. */
function makeResponse(spec: ResponseSpec): Response {
  switch (spec.shape.kind) {
    case 'empty':
      return new Response(null, { status: spec.status });
    case 'nonJsonText':
      return new Response(spec.shape.text, {
        status: spec.status,
        headers: { 'Content-Type': 'text/html' },
      });
    case 'plainJson':
      return new Response(JSON.stringify(spec.shape.value), {
        status: spec.status,
        headers: { 'Content-Type': 'application/json' },
      });
    case 'envelope': {
      const body = spec.shape.asString
        ? JSON.stringify(spec.shape.inner)
        : spec.shape.inner;
      return new Response(JSON.stringify({ statusCode: spec.shape.statusCode, body }), {
        status: spec.status,
        headers: { 'Content-Type': 'application/json' },
      });
    }
  }
}

type Outcome = { rejects: false } | { rejects: true; status: number };

/**
 * What R3 requires, written from the criteria rather than from `_unwrap`.
 *
 * The classifier is the status in every branch; what differs is **whose** status
 * counts as the response's own, and that is the whole content of the property.
 */
function requiredOutcome(spec: ResponseSpec): Outcome {
  const httpOk = spec.status >= 200 && spec.status < 300;

  switch (spec.shape.kind) {
    case 'empty':
    case 'nonJsonText':
      // R3.1 — an unparseable body must raise an error carrying the response's
      // HTTP status. R3.2 for >= 400 (classify by that status regardless of
      // whether the body parsed) and R3.3 for < 400 (not an HTTP error, so the
      // error names the body instead). Either way the status is the HTTP one:
      // there is no parsed body to carry a second opinion.
      return { rejects: true, status: spec.status };

    case 'plainJson':
      // R3.4 — the parse-succeeded arm is unchanged, and unchanged means the
      // plain branch classifies on `!res.ok`. `ok` is 200-299, so the 3xx band
      // rejects too. (`fetch` follows redirects by default, so a 3xx reaching
      // `_unwrap` is unusual in production; it is representable, so it is swept.)
      return httpOk ? { rejects: false } : { rejects: true, status: spec.status };

    case 'envelope':
      // R3.2 + R3.4 — the envelope is the response's own statement about itself,
      // made by the handler that answered. So `>= 400` in the envelope raises
      // with the ENVELOPE's status even when the HTTP status disagrees, and an
      // envelope declaring `< 400` resolves even over a failing HTTP status.
      // Both directions of that disagreement are reachable here.
      return spec.shape.statusCode >= 400
        ? { rejects: true, status: spec.shape.statusCode }
        : { rejects: false };
  }
}

/**
 * How many times the one-shot unauthorized handler must be invoked for this
 * response — the exact number, of which "at most once" (R3.4) is the upper half.
 *
 * A healthy response invokes it **zero** times, which is why this is an expected
 * count rather than a bare `<= 1`: asserting only the ceiling would still pass if
 * the redirect were lost altogether, and losing it strands an operator on an
 * expired session. Both 401 shapes count, and a response carrying **both** still
 * counts once, because `isRedirecting` collapses `requestRaw`'s trigger
 * (`api-client.ts:137`) and `_unwrap`'s (`:203`) into one redirect.
 */
function requiredUnauthorizedCalls(spec: ResponseSpec): 0 | 1 {
  const realUnauthorized = spec.status === 401;
  const wrappedUnauthorized =
    spec.shape.kind === 'envelope' && spec.shape.statusCode === 401;
  return realUnauthorized || wrappedUnauthorized ? 1 : 0;
}

/** The set of statuses the RESPONSE declared. Nothing outside it may be raised. */
function statusesTheResponseDeclared(spec: ResponseSpec): number[] {
  return spec.shape.kind === 'envelope'
    ? [spec.status, spec.shape.statusCode]
    : [spec.status];
}

function invoke(client: ApiClient, spec: ResponseSpec): Promise<unknown> {
  switch (spec.method) {
    case 'get':
      return client.get(spec.path);
    case 'post':
      return client.post(spec.path, { a: 1 });
    case 'put':
      return client.put(spec.path, { a: 1 });
    case 'delete':
      return client.delete(spec.path);
  }
}

// ---------------------------------------------------------------------------
// Feature: p54-reused-and-skipped-run-honesty, Property 3
// ---------------------------------------------------------------------------

describe("Feature: p54-reused-and-skipped-run-honesty, Property 3: A raised API error always carries the response's own status", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('raises ApiError carrying the response\u2019s own status for every status \u00d7 body shape, and triggers the unauthorized handler at most once per response', async () => {
    await fc.assert(
      fc.asyncProperty(responseSpec, async (spec) => {
        // A FRESH client per iteration. `isRedirecting` is per-instance one-shot
        // state, so a shared client would make the handler-count assertion pass
        // for the wrong reason from the second 401 onwards.
        const client = new ApiClient('');
        const onUnauthorized = vi.fn();
        client.setUnauthorizedHandler(onUnauthorized);

        // A factory, so the request gets a fresh `Response` rather than a used one.
        vi.stubGlobal(
          'fetch',
          vi.fn(() => Promise.resolve(makeResponse(spec))),
        );

        const settled = await invoke(client, spec).then(
          (value) => ({ ok: true as const, value }),
          (error: unknown) => ({ ok: false as const, error }),
        );

        const required = requiredOutcome(spec);

        if (required.rejects) {
          expect(settled.ok).toBe(false);
          const err = (settled as { ok: false; error: unknown }).error;

          // R3.4's load-bearing consequence: 19 non-test sites discriminate with
          // `err instanceof ApiError`. An error of any other class — and
          // `SyntaxError` is the one this spec removed — is invisible to all of
          // them, so the class is asserted on every rejection, not just the ones
          // whose status is interesting.
          expect(err).toBeInstanceOf(ApiError);
          expect(err).not.toBeInstanceOf(SyntaxError);

          const apiErr = err as ApiError;

          // R3.1/R3.2/R3.3 — the exact status the criteria require.
          expect(apiErr.status).toBe(required.status);

          // The same claim again, oracle-independently: whatever status is
          // carried, the RESPONSE declared it. If the oracle above were wrong,
          // this still fails on a status the client invented.
          expect(statusesTheResponseDeclared(spec)).toContain(apiErr.status);

          // An `ApiError` always reaches a toast with something to say — the
          // constructor's `API error ${status}` fallback guarantees it even when
          // the body carried no `message` and no `error`.
          expect(typeof apiErr.message).toBe('string');
          expect(apiErr.message.length).toBeGreaterThan(0);
        } else {
          // R3.4 — the parse-succeeded, non-error arm still resolves. A property
          // that only checked rejections would be satisfied by a client that
          // threw on everything.
          expect(settled.ok).toBe(true);
        }

        // R3.4's one-shot clause, per response, across every shape — including
        // the shapes where it must be zero.
        expect(onUnauthorized).toHaveBeenCalledTimes(requiredUnauthorizedCalls(spec));
        expect(onUnauthorized.mock.calls.length).toBeLessThanOrEqual(1);

        // The in-flight map is cleaned up whichever way the request settled, so
        // a rejection cannot poison the next GET of the same URL.
        expect(client.inflightSize).toBe(0);
      }),
      { numRuns: 100 },
    );
  });

  it('invokes the unauthorized handler exactly once per response for either 401 shape, and for a response carrying both', async () => {
    // R3.4's one-shot clause, swept over all three unauthorized constructions
    // rather than left to the ~7% of general draws that happen to be a 401.
    await fc.assert(
      fc.asyncProperty(unauthorizedSpec, async (spec) => {
        const client = new ApiClient('');
        const onUnauthorized = vi.fn();
        client.setUnauthorizedHandler(onUnauthorized);
        vi.stubGlobal(
          'fetch',
          vi.fn(() => Promise.resolve(makeResponse(spec))),
        );

        const settled = await invoke(client, spec).then(
          () => null,
          (error: unknown) => error,
        );

        // At most once — and not zero either, because a lost redirect strands an
        // operator on an expired session and no other assertion would notice.
        expect(onUnauthorized).toHaveBeenCalledTimes(1);

        // Whatever else happened, the outcome still obeys the status rule. A
        // wrapped 401 rejects (401 >= 400); a real HTTP 401 whose envelope
        // declares `< 400` resolves, which the general property also reaches —
        // physically impossible in production, since the Cognito authorizer's 401
        // never carries a handler envelope, but representable, so it is swept
        // rather than excluded.
        const required = requiredOutcome(spec);
        if (required.rejects) {
          expect(settled).toBeInstanceOf(ApiError);
          expect((settled as ApiError).status).toBe(required.status);
        } else {
          expect(settled).toBeNull();
        }
      }),
      { numRuns: 100 },
    );
  });

  it('never invokes the unauthorized handler when no 401 shape arrived, at any other status', async () => {
    // The complement, drawn on purpose rather than left to chance: the assertion
    // above is exact, but 401 is dense in the weighted generator, so this pins
    // the zero case over the rest of the range where a spurious redirect would
    // log an operator out of a working session.
    await fc.assert(
      fc.asyncProperty(
        responseSpec.filter((spec) => requiredUnauthorizedCalls(spec) === 0),
        async (spec) => {
          const client = new ApiClient('');
          const onUnauthorized = vi.fn();
          client.setUnauthorizedHandler(onUnauthorized);
          vi.stubGlobal(
            'fetch',
            vi.fn(() => Promise.resolve(makeResponse(spec))),
          );

          await invoke(client, spec).catch(() => undefined);

          expect(onUnauthorized).not.toHaveBeenCalled();
        },
      ),
      { numRuns: 100 },
    );
  });

  it('raises ApiError rather than TypeError for every status \u00d7 shape when no handler is registered', async () => {
    // `onUnauthorized` is null until the auth layer wires it at startup. Every
    // hook mounted before that wiring depends on a 401 still rejecting with
    // `ApiError` rather than a `TypeError` from an unguarded call — 0.1 pins this
    // for one parseable 401 body; here it is swept, because the unparseable arms
    // 1.1 added reach the same `handleUnauthorized` through `requestRaw`.
    await fc.assert(
      fc.asyncProperty(responseSpec, async (spec) => {
        const client = new ApiClient('');
        vi.stubGlobal(
          'fetch',
          vi.fn(() => Promise.resolve(makeResponse(spec))),
        );

        const settled = await invoke(client, spec).then(
          () => null,
          (error: unknown) => error,
        );

        if (requiredOutcome(spec).rejects) {
          expect(settled).toBeInstanceOf(ApiError);
        } else {
          expect(settled).toBeNull();
        }
      }),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// The residual, asserted as a residual
// ---------------------------------------------------------------------------

describe('_unwrap residual: an envelope whose inner body STRING is malformed (NOT part of Property 3)', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /**
   * **This is a documented gap, not a requirement.** `api-client.ts:23-25` names
   * it: `JSON.parse(raw.body)` at the wrapper branch sits **inside** the
   * parse-succeeded arm R3.4 pins as unchanged, so an envelope whose inner `body`
   * string is not valid JSON still escapes as `SyntaxError`. Task 1.1 left it
   * there deliberately and task 1.4 is not permitted to change `api-client.ts`.
   *
   * It is pinned here so the residual cannot move without a test noticing, and so
   * a reader does not mistake Property 3's generator — which is filtered clear of
   * this shape — for coverage of it. Whoever closes it deletes this block.
   *
   * **The residual is wider than that sentence reads, and Property 3's own
   * generator is what showed it.** The first draft drew
   * `{ statusCode: 200, body: '' }` (fast-check seed 441932654, shrunk 6 times
   * from `{status: 200, shape: {kind: 'envelope', statusCode: 200, inner: '',
   * asString: false}}`) and got `SyntaxError: Unexpected end of JSON input` where
   * the oracle required a resolve. `''` is not a *malformed serialisation* — it is
   * a handler returning an empty-string body, and `'OK'` is a handler returning a
   * word. The wrapper branch cannot tell either from a serialised JSON string,
   * because its only test is `typeof raw.body === 'string'`. So the residual is
   * **any** envelope whose `body` is a bare string that is not itself valid JSON,
   * not only a truncated or corrupt serialisation. Both are asserted below.
   */
  it('still escapes as SyntaxError for a truncated inner serialisation, which the 19 instanceof-ApiError sites cannot see', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ statusCode: 200, body: '{"truncated": ' }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        ),
      ),
    );

    const err = await new ApiClient('').get('/malformed-inner').catch((e: unknown) => e);

    expect(err).toBeInstanceOf(SyntaxError);
    expect(err).not.toBeInstanceOf(ApiError);
  });

  it.each([
    ['an empty-string body', ''],
    ['a bare word body', 'OK'],
  ])(
    'also escapes as SyntaxError for %s, which is a plain value rather than a corrupt serialisation',
    async (_label, innerBody) => {
      // The counterexample above, and its generalisation. Recorded rather than
      // fixed: closing it means changing `api-client.ts`, which task 1.4 may not
      // do, and it sits inside the arm R3.4 pins.
      vi.stubGlobal(
        'fetch',
        vi.fn(() =>
          Promise.resolve(
            new Response(JSON.stringify({ statusCode: 200, body: innerBody }), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            }),
          ),
        ),
      );

      const err = await new ApiClient('').get('/plain-string-inner').catch((e: unknown) => e);

      expect(err).toBeInstanceOf(SyntaxError);
      expect(err).not.toBeInstanceOf(ApiError);
    },
  );
});
