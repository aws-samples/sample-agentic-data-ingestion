// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * MetricsDashboardPage — the consumer half of the cross-layer seam test
 * (p39 task 20.2, design `D2`).
 *
 * **What this file exists to catch.** `MetricsDashboardPage.trend-polarity.test.tsx:68-79`
 * and `MetricsDashboardPage.property.test.tsx` both module-mock the `../../hooks`
 * barrel and hand `TrendIndicators` values straight to the component. They are good
 * tests of the frontend's own contract, they stay exactly as they are, and
 * neither can see a backend that has already flipped the direction — no API is
 * involved, so a flipped direction is unobservable. `R9.1` closes that gap by
 * requiring the data to arrive the way it arrives in production: through the
 * real hook, from a payload the handler produced.
 *
 * **So this file mocks the transport and nothing above it.** There is no module
 * mock of the hooks barrel anywhere below — `useMetrics`, `fetchMetrics`,
 * `apiClient.get` and its Lambda-proxy unwrapping all execute for real. Task
 * 20.4's guard at the bottom of this file scans this file's own source for such
 * a mock. It parses rather than greps, so the prose is free to name the
 * forbidden call literally — `vi.mock('../../hooks', …)` — and the assertion at
 * `documents the fence` below pins that freedom in place. The only substitution
 * is `globalThis.fetch`, which answers `/api/metrics/comprehensive` with a
 * fixture that the producer half (`tests/contracts/api_responses/test_metrics_trend_seam.py`)
 * generated from `get_comprehensive_metrics`. The request URL is asserted below
 * so "the real hook ran" is evidence rather than a claim. CodeGraph flagged
 * `useMetrics` and `fetchMetrics` as having no covering tests; this is their
 * first exercise.
 *
 * **Where the expectations come from.** Not from rendering and copying the
 * output — that output is its own oracle (`LGR-28b4d75-5`, template / ratified).
 * Cost is lower-is-better (`LGR-99050e6-1`, template / ratified), so a rise is
 * the negative signal and a fall the positive one. `R9.2` therefore requires
 * `up` + `text-status-error` and `R9.3` requires `down` + `text-status-success`.
 * A backend that inverted the direction would flip the fixture, and the fixture
 * flip lands on the wrong side of that rule — which is the whole point of the
 * arrangement.
 *
 * **The fixture is read from disk, not copied here.** It lives outside
 * `dashboard-react`, under `agentic_data_ingestion/tests/fixtures/api_responses/`,
 * because task 20.3's freshness guard regenerates and diffs those same two
 * files. A copy inside `dashboard-react` would leave the guard watching a file
 * nothing renders, and this test asserting a hand-written one.
 *
 * **Validates: Requirements 9.1, 9.2, 9.3**
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import ts from 'typescript';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import MetricsDashboardPage from '../MetricsDashboardPage';

/** The endpoint the real `fetchMetrics` builds, `/api` base URL included. */
const METRICS_URL = '/api/metrics/comprehensive?period=24h';

/**
 * Read a producer-generated payload from the pytest fixture directory, four
 * levels up and outside this package. `readFileSync` at run time rather than a
 * JSON `import`: an import would need a vite `resolve.alias` or a
 * `server.fs.allow` entry to reach outside `dashboard-react`, and would be
 * resolved by the bundler at transform time. Reading it keeps `vite.config.ts`
 * untouched.
 *
 * The `/@fs` strip is vite's, not ours: under vitest `import.meta.url` is
 * `file:///@fs/<abs path>`, so resolving a relative URL against it yields a real
 * absolute path behind vite's filesystem-access prefix. Removing the prefix
 * gives the path `fs` wants, and is a no-op wherever `import.meta.url` is a
 * plain `file://` URL.
 */
function loadProducerFixture(name: string): unknown {
  const resolved = fileURLToPath(new URL(`../../../../tests/fixtures/api_responses/${name}`, import.meta.url));
  return JSON.parse(readFileSync(resolved.replace(/^\/@fs/, ''), 'utf-8'));
}

/**
 * Replace only the transport. Everything between `useMetrics` and this stub —
 * the query key, `fetchMetrics`, `apiClient.get`, the proxy unwrap — is the
 * production code path.
 *
 * URLs other than the metrics endpoint get a 500: the two other queries on this
 * page (token usage, cost recommendations) are not part of this seam and have
 * no fixture, so they render their own honest error widgets. Both resolve
 * without retrying (`useCostRecommendations` sets `retry: false`; the token
 * query inherits the client default set below).
 */
function stubTransport(payload: unknown): string[] {
  const requested: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requested.push(url);
      const body = url.startsWith('/api/metrics/comprehensive') ? payload : { error: 'not part of this seam' };
      const status = url.startsWith('/api/metrics/comprehensive') ? 200 : 500;
      return new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });
    }),
  );
  return requested;
}

/**
 * A fresh client per test with retries off, so a transport failure surfaces as
 * a failed assertion immediately instead of after a retry schedule, and so no
 * cached payload leaks from the rise case into the fall case.
 */
function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <MetricsDashboardPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/**
 * The Cloudscape colour token carried by a tile's arrow, e.g.
 * "text-status-error" — the `Box color` prop lands in the DOM as an
 * `awsui_color-<token>_<hash>` class. Same extraction as
 * `trend-polarity.test.tsx:89-94`, widened to admit `inactive` exactly as
 * `not-established.test.tsx:274-276` does, because the three non-cost trends in
 * both fixtures are `not_established`.
 */
function arrowColorToken(testId: string): string {
  const cls = screen.getByTestId(`${testId}-arrow`).className;
  const match = /awsui_color-(text-status-(?:success|error|info|inactive))_/.exec(cls);
  expect(match, `no colour token class on ${testId}-arrow (class="${cls}")`).not.toBeNull();
  return match![1];
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('MetricsDashboardPage — cross-layer seam, real useMetrics (R9.1)', () => {
  it('renders a producer-generated cost rise as up and a negative signal (R9.2)', async () => {
    const fixture = loadProducerFixture('get_metrics.trend_up.json');
    // The direction under test is the handler's, not this file's. Stated as a
    // precondition so a fixture that drifted fails by name rather than by a
    // confusing colour mismatch.
    expect(
      (fixture as { performance: { trends: { cost: string } } }).performance.trends.cost,
      'the rise fixture must carry cost trending up',
    ).toBe('up');

    const requested = stubTransport(fixture);
    renderPage();

    await screen.findByTestId('trend-cost');

    // Evidence that the real hook ran: the URL was built by `fetchMetrics`,
    // prefixed by the real `ApiClient` base URL. A mocked hook issues nothing.
    expect(requested).toContain(METRICS_URL);

    expect(screen.getByTestId('trend-cost').textContent).toContain('Up');
    expect(screen.getByTestId('trend-cost-arrow').textContent).toContain('↑');
    // Cost is lower-is-better, so the rise is bad news — and the frontend is
    // what says so.
    expect(arrowColorToken('trend-cost')).toBe('text-status-error');
  });

  it('renders a producer-generated cost fall as down and a positive signal (R9.3)', async () => {
    const fixture = loadProducerFixture('get_metrics.trend_down.json');
    expect(
      (fixture as { performance: { trends: { cost: string } } }).performance.trends.cost,
      'the fall fixture must carry cost trending down',
    ).toBe('down');

    const requested = stubTransport(fixture);
    renderPage();

    await screen.findByTestId('trend-cost');

    expect(requested).toContain(METRICS_URL);

    expect(screen.getByTestId('trend-cost').textContent).toContain('Down');
    expect(screen.getByTestId('trend-cost-arrow').textContent).toContain('↓');
    expect(arrowColorToken('trend-cost')).toBe('text-status-success');
  });
});
// ---------------------------------------------------------------------------
// Task 20.4 — R9.1's no-mock condition, asserted against this file's own source
// ---------------------------------------------------------------------------
//
// The two render tests above are only a seam test for as long as the data keeps
// arriving through the production path. Reintroducing `vi.mock('../../hooks', …)`
// here during a later refactor would leave both tests green while quietly
// deleting what they exist to check: a backend that flipped the direction would
// again be unobservable. This guard makes that regression a failing test.
//
// **Detection is AST-based, not substring-based, and that is load-bearing.**
// `LGR-b46a5bb-4` (template / ratified) chose exactly this for the sibling
// Python guard, on the ground that substring matching "forbids documenting the
// fence" — it had already failed against `handlers/agents.py`, whose docstring
// names the antipattern on purpose. The same trap is live here: the header
// comment above and this paragraph both name `vi.mock('../../hooks', …)`
// literally, and the `VIOLATING_SOURCES` table below embeds it in string
// literals. A grep for the call would fail this file on its own documentation.
// TypeScript's parser is already in `devDependencies` (`typescript` ~5.6), so no
// dependency was added for this.
//
// **What the detector can see.** Comments are trivia and string literals are
// leaves, so both are excluded structurally rather than by luck. It resolves the
// callee — `vi.mock` / `vi.doMock` / `vi.unstable_mockModule`, under an import
// alias, under `vitest.vi.*`, and through `vi['mock']` — then reads the module
// specifier from a string literal, a template literal with no substitutions, or
// vitest 3's `vi.mock(import('…'))` form. A specifier it cannot resolve
// statically is reported rather than skipped, so hiding the target behind a
// variable fails too.
//
// **What it cannot see.** It is a single-file syntactic check, not a type-aware
// one: it has no module resolution, so it matches specifiers by path shape, and
// a mock installed from an imported helper, from `setupFiles`, or from
// `__mocks__/` directory conventions is outside its reach. It also says nothing
// about the other seven metrics page test files — deliberately, per task 20.4's
// scope. `trend-polarity.test.tsx:65`, `property.test.tsx:24` and
// `not-established.test.tsx:144` all keep their mock; they test the frontend's
// own contract and a directory-wide guard would be wrong, not stricter.
//
// **What it must not forbid.** `vi.stubGlobal('fetch', …)` is what R9.1 permits
// and what the tests above depend on. Only module mocks on the metrics data path
// are findings.
//
// **Validates: Requirements 9.1**

/** This file's own source, read at run time. Same `/@fs` handling as `loadProducerFixture`. */
function ownSource(): string {
  return readFileSync(fileURLToPath(import.meta.url).replace(/^\/@fs/, ''), 'utf-8');
}

/** Vitest's module-mocking entry points. `stubGlobal` is deliberately absent. */
const MOCK_METHODS = new Set(['mock', 'doMock', 'unstable_mockModule']);

/**
 * Modules whose replacement would break the production data path this file
 * exists to exercise: the hooks barrel and anything under it (so
 * `../../hooks/use-metrics`, where `fetchMetrics` lives, is covered by name),
 * plus the transport client one layer down.
 */
const FORBIDDEN_BASENAMES = new Set(['use-metrics', 'api-client']);

interface MockFinding {
  line: number;
  callee: string;
  specifier: string;
  reason: string;
}

/** Names in scope that hold vitest's `vi` object, or the vitest module itself. */
function vitestBindings(sf: ts.SourceFile): { vi: Set<string>; ns: Set<string> } {
  // `test.globals: true` in vite.config.ts puts `vi` in scope with no import,
  // so the bare names are always candidates.
  const vi = new Set<string>(['vi']);
  const ns = new Set<string>(['vitest']);

  ts.forEachChild(sf, (node) => {
    if (!ts.isImportDeclaration(node) || !ts.isStringLiteral(node.moduleSpecifier)) return;
    if (node.moduleSpecifier.text !== 'vitest') return;
    const clause = node.importClause;
    if (!clause) return;
    if (clause.name) ns.add(clause.name.text);
    const bindings = clause.namedBindings;
    if (!bindings) return;
    if (ts.isNamespaceImport(bindings)) {
      ns.add(bindings.name.text);
      return;
    }
    for (const element of bindings.elements) {
      const imported = (element.propertyName ?? element.name).text;
      if (imported === 'vi') vi.add(element.name.text);
    }
  });

  return { vi, ns };
}

/** `vi.mock` and friends, however the receiver was spelled. `null` for anything else. */
function mockCallee(call: ts.CallExpression, bound: { vi: Set<string>; ns: Set<string> }): string | null {
  const target = call.expression;
  let method: string | null = null;
  let receiver: ts.Expression | null = null;

  if (ts.isPropertyAccessExpression(target)) {
    method = target.name.text;
    receiver = target.expression;
  } else if (ts.isElementAccessExpression(target) && target.argumentExpression && ts.isStringLiteralLike(target.argumentExpression)) {
    method = target.argumentExpression.text;
    receiver = target.expression;
  }
  if (method === null || receiver === null || !MOCK_METHODS.has(method)) return null;

  // `vi.mock(…)`, or an aliased `vi`, or the namespace object used directly.
  if (ts.isIdentifier(receiver) && (bound.vi.has(receiver.text) || bound.ns.has(receiver.text))) {
    return `${receiver.text}.${method}`;
  }
  // `vitest.vi.mock(…)`.
  if (
    ts.isPropertyAccessExpression(receiver) &&
    receiver.name.text === 'vi' &&
    ts.isIdentifier(receiver.expression) &&
    bound.ns.has(receiver.expression.text)
  ) {
    return `${receiver.expression.text}.vi.${method}`;
  }
  return null;
}

/** The mocked module specifier, or `null` when it is not statically knowable. */
function staticSpecifier(arg: ts.Expression | undefined): string | null {
  if (!arg) return null;
  if (ts.isStringLiteral(arg) || ts.isNoSubstitutionTemplateLiteral(arg)) return arg.text;
  // vitest 3's `vi.mock(import('../../hooks'))`.
  if (ts.isCallExpression(arg) && arg.expression.kind === ts.SyntaxKind.ImportKeyword) {
    return staticSpecifier(arg.arguments[0]);
  }
  return null;
}

/** Why this specifier is off limits, or `null` if mocking it is nobody's business here. */
function forbiddenReason(specifier: string): string | null {
  const segments = specifier
    .split('?')[0]
    .replace(/\.(?:m|c)?[jt]sx?$/, '')
    .split('/')
    .filter((s) => s !== '' && s !== '.' && s !== '..' && s !== '@' && s !== '~');
  if (segments.length === 0) return null;
  if (segments.includes('hooks')) return 'the data hook module — R9.1 requires the real useMetrics';
  const last = segments[segments.length - 1];
  if (FORBIDDEN_BASENAMES.has(last)) return `${last}, a module on the metrics data path below useMetrics`;
  return null;
}

/**
 * Every module mock in `source` that would take the metrics data path out of
 * play. Exported shape is `(line, callee, specifier, reason)` so a failure names
 * the offending line rather than just asserting a count.
 */
function findForbiddenModuleMocks(source: string): MockFinding[] {
  const sf = ts.createSourceFile('seam.test.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const bound = vitestBindings(sf);
  const findings: MockFinding[] = [];

  const visit = (node: ts.Node): void => {
    if (ts.isCallExpression(node)) {
      const callee = mockCallee(node, bound);
      if (callee !== null) {
        const line = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
        const specifier = staticSpecifier(node.arguments[0]);
        if (specifier === null) {
          findings.push({
            line,
            callee,
            specifier: node.arguments[0] ? node.arguments[0].getText(sf) : '<no argument>',
            reason: 'the mocked module cannot be determined statically, so this guard cannot clear it',
          });
        } else {
          const reason = forbiddenReason(specifier);
          if (reason !== null) findings.push({ line, callee, specifier, reason });
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);

  return findings;
}

/** Global names handed to `vi.stubGlobal` — used to show the transport stub survives. */
function findStubbedGlobals(source: string): string[] {
  const sf = ts.createSourceFile('seam.test.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const bound = vitestBindings(sf);
  const names: string[] = [];

  const visit = (node: ts.Node): void => {
    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      node.expression.name.text === 'stubGlobal' &&
      ts.isIdentifier(node.expression.expression) &&
      bound.vi.has(node.expression.expression.text)
    ) {
      const name = staticSpecifier(node.arguments[0]);
      if (name !== null) names.push(name);
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);

  return names;
}

const FINDINGS_HINT =
  'R9.1 requires this file to reach the component through the real useMetrics, so a flipped ' +
  'backend direction is observable. Replacing the hook, use-metrics or api-client makes both ' +
  'render tests above pass without testing the seam at all. Stub the transport instead — ' +
  "vi.stubGlobal('fetch', …), as stubTransport does. The mock belongs in trend-polarity.test.tsx " +
  'or property.test.tsx, which test the frontend contract on purpose.';

/** Each of these must be caught; the shapes a later refactor would plausibly reach for. */
const VIOLATING_SOURCES: ReadonlyArray<readonly [string, string]> = [
  ['the barrel, exactly as trend-polarity.test.tsx:65 spells it', "import { vi } from 'vitest';\nvi.mock('../../hooks', () => ({ useMetrics: () => ({ data: {} }) }));\n"],
  ['the hook module directly', "vi.mock('../../hooks/use-metrics');\n"],
  ['the transport client one layer down', "vi.mock('../../services/api-client', () => ({ apiClient: { get: async () => ({}) } }));\n"],
  ['the path alias form', "vi.mock('@/hooks', () => ({}));\n"],
  ['an explicit extension', "vi.mock('../../hooks/use-metrics.ts', () => ({}));\n"],
  ['vi under an import alias', "import { vi as v } from 'vitest';\nv.mock('../../hooks', () => ({}));\n"],
  ['the namespace form', "import * as vitest from 'vitest';\nvitest.vi.mock('../../hooks', () => ({}));\n"],
  ['element access on vi', "vi['mock']('../../hooks', () => ({}));\n"],
  ['doMock, which defers but replaces just the same', "vi.doMock('../../hooks', () => ({}));\n"],
  ['a template literal specifier', 'vi.mock(`../../hooks`, () => ({}));\n'],
  ["vitest 3's import() form", "vi.mock(import('../../hooks'));\n"],
  ['the target hidden behind a variable', "const target = '../../hooks';\nvi.mock(target, () => ({}));\n"],
];

/** These must NOT be caught. Documenting or discussing the fence is not building one. */
const INNOCENT_SOURCES: ReadonlyArray<readonly [string, string]> = [
  [
    'a comment naming the forbidden call literally',
    "// Do NOT reintroduce vi.mock('../../hooks', () => ({ useMetrics })) here.\n/* vi.mock('../../hooks') was the old shape. */\n",
  ],
  ['a docblock naming it', "/**\n * There is no vi.mock('../../hooks', …) below, and that is the point.\n */\nexport const x = 1;\n"],
  ['the same text inside a string literal', "const forbidden = \"vi.mock('../../hooks', () => ({}))\";\nexport { forbidden };\n"],
  ['the transport stub R9.1 permits', "vi.stubGlobal('fetch', vi.fn(async () => new Response('{}')));\n"],
  ['a mock of something off the data path', "vi.mock('react-router-dom', () => ({ useNavigate: () => () => {} }));\n"],
];

describe('MetricsDashboardPage seam — no module mock of the data path (R9.1, task 20.4)', () => {
  it('reads a real, non-trivial source, so the scan below is not vacuous', () => {
    const source = ownSource();
    expect(source.length).toBeGreaterThan(4000);
    expect(source, 'ownSource() did not read this file').toContain(
      'MetricsDashboardPage seam — no module mock of the data path',
    );
  });

  it('this file module-mocks nothing on the metrics data path', () => {
    const findings = findForbiddenModuleMocks(ownSource());
    const rendered = findings.map((f) => `  line ${f.line}: ${f.callee}('${f.specifier}') — ${f.reason}`).join('\n');
    expect(findings, `Forbidden module mock in the seam test:\n${rendered}\n\n${FINDINGS_HINT}`).toEqual([]);
  });

  it.each(VIOLATING_SOURCES)('catches %s', (_label, source) => {
    expect(findForbiddenModuleMocks(source).length, `detector missed a violation in:\n${source}`).toBeGreaterThan(0);
  });

  it.each(INNOCENT_SOURCES)('permits %s', (_label, source) => {
    expect(findForbiddenModuleMocks(source), `detector produced a false positive on:\n${source}`).toEqual([]);
  });

  it('documents the fence by naming the forbidden call, and still passes — the LGR-b46a5bb-4 property', () => {
    // The live instance of the synthetic case above, pinned against this file.
    // If the prose that names the call is ever removed, this assertion has
    // stopped guarding anything and should be deleted deliberately, not left to
    // pass hollowly.
    const source = ownSource();
    expect(
      source,
      'this file no longer names the forbidden call in prose, so the substring-vs-AST distinction is no longer being exercised here',
    ).toContain("vi.mock('../../hooks'");
    expect(findForbiddenModuleMocks(source)).toEqual([]);
  });

  it('still permits the transport stub the tests above depend on', () => {
    // Not merely "does not forbid it" in the abstract: this file really does
    // stub fetch, and the guard really does clear it.
    expect(findStubbedGlobals(ownSource())).toContain('fetch');
  });
});
