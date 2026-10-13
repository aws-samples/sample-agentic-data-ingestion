// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import { ComputeStack } from '../lib/compute-stack';

/**
 * `dashboard_api` is sized 512 MB / 25 s, and both figures are pinned with their
 * basis — p47 R11.1, R11.2, R11.3; design §4.8.
 *
 * WHY THIS EXISTS
 * ---------------
 * R11 asked for the memory to be "set from a measurement, not a guess". Task D14
 * took the measurement and the answer was **keep 512 MB**. A confirmed value and
 * an unexamined value look identical in the source, so without this file the next
 * reader of R11 finds a bare `memorySize: 512` and re-opens a settled question —
 * or worse, "optimises" it in either direction without the numbers.
 *
 * THE MEASUREMENT, in brief (full method and limits in
 * `.kiro/specs/p47-operative-gaps-and-hot-path-efficiency/decision-records/D14-dashboard-api-memory.txt`):
 *
 *  - 1889 real REPORT lines, 444455556666 / us-west-2, 2026-07-21..08-23,
 *    read-only. MaxMemoryUsed min 176 / p50 202 / p99 240 / **max 258 MB** of 512.
 *  - Zero `Task timed out`, `Runtime exited`, `OutOfMemory` or `signal: killed`
 *    events in the log group's entire history. Max Duration 6262 ms against 25 s.
 *  - Largest marginal per-request Python heap on the post-R7/R8 code, measured
 *    locally with tracemalloc over moto: **20.0 MB**.
 *
 * WHY 512 RATHER THAN LOWER OR HIGHER
 * -----------------------------------
 * 256 MB is excluded by evidence: it is below the 258 MB already observed, so it
 * would have been an OOM kill on at least one real invocation in the window.
 *
 * 384 MB nets only ~0.80x the cost, not the 0.75x the rate card suggests, because
 * Lambda vCPU scales linearly with memory (512 MB = 0.29 vCPU of the 1769 MB
 * full-vCPU point) and cold-start init — 20.4% of billed duration here — is
 * CPU-bound and would lengthen 1.33x. A ~20% saving on a function billing ~1207 s
 * per month is not worth halving the headroom over a maximum drawn from a
 * low-traffic dev account.
 *
 * Raising it fails on a structural argument rather than a measured one: for work
 * whose duration scales linearly with CPU, a memory increase is exactly
 * cost-NEUTRAL — double the per-ms rate, half the ms — so memory buys latency and
 * never cost. This function's expensive tail is a `ThreadPoolExecutor` fan-out
 * plus DynamoDB pagination, which is network-bound and does not shorten with CPU
 * at all, so 1024 MB (needing a 50% duration cut to break even) would be a
 * straight ~1.94x. Even granting init a perfect 3.46x speedup at 1769 MB, the
 * total lands at 2.84x.
 *
 * WHAT IS DELIBERATELY NOT CLAIMED
 * --------------------------------
 * No duration-vs-memory curve. Producing one means deploying at several sizes and
 * re-driving traffic, which D14 was not authorised to do. So the numbers above
 * BOUND the decision — they show 512 is safe and that no candidate has a
 * measured case — rather than prove 512 optimal. The revisit conditions are in the
 * decision record.
 *
 * WHY THE TIMEOUT IS ASSERTED HERE TOO
 * ------------------------------------
 * R11.3 forbids raising the 25 s timeout as a substitute for R7/R8, and the
 * coupling is concrete, not deferential. Three in-code budgets are derived from
 * that ceiling as "25 s minus 5 s in hand" and all read 20.0:
 * `stats.py:_STATS_TIME_BUDGET_SECONDS`, `schemas.py:_LISTING_TIME_BUDGET_SECONDS`,
 * `pipelines_crud.py:_LIST_TIME_BUDGET_SECONDS`. `LGR-14d6dd3-2` (template /
 * ratified) bounds the Display_Label fan-out "by a wall-clock deadline derived at
 * call time from the request's remaining budget", cites this Lambda's 25 s timeout
 * by name, and warns that a third timer would be "a third thing to keep consistent
 * with the Lambda's 25 s ceiling" (`pipelines_crud.py:256`).
 *
 * So raising the timeout moves none of those three constants: the degradation
 * markers `partial_counts` and `enrichment_abandoned` would still fire at 20 s and
 * the endpoint would still return at 20 s. It buys nothing while invalidating the
 * figure they were derived from — and 25 s exists below API Gateway's 29 s
 * integration ceiling, past which the gateway returns 504 with no body at all,
 * which is the outcome those markers exist to avoid.
 *
 * WHY THE SYNTHESIZED TEMPLATE, NOT THE SOURCE
 * --------------------------------------------
 * `LGR-1166391-1` (template / ratified) settled this for CDK-shape guards: assert
 * on what CloudFormation is handed, because a source-text scan can pass on input
 * that still fails at deploy. It also recorded that the Python `tests/cdk/` suite
 * cannot render a template (`aws-cdk-lib` is absent from `pyproject.toml`), so
 * vitest is the only suite that can make this assertion — the same resolution
 * `p47-strands-layer-attachment.test.ts` reached for the layer property.
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';

const DASHBOARD_API_FUNCTION_NAME = 'agentic_data_ingestion_dashboard_api';

/** D14's measured value. Changing it requires a fresh measurement — see header. */
const MEASURED_MEMORY_MB = 512;

/** Highest MaxMemoryUsed observed across the 1889-invocation window. */
const OBSERVED_PEAK_MEMORY_MB = 258;

/**
 * R11.3's value. Load-bearing: three in-code budgets are derived from it, and it
 * sits below API Gateway's 29 s integration ceiling.
 */
const REQUIRED_TIMEOUT_SECONDS = 25;

/** API Gateway's REST integration ceiling — the reason 25 is not 30. */
const API_GATEWAY_INTEGRATION_CEILING_SECONDS = 29;

function synthesizeCompute(): Record<string, any> {
  // Bundling disabled for the same reason `p47-strands-layer-attachment.test.ts`
  // disables it (`LGR-1166391-1`): bundling exhausts the vitest worker, and asset
  // content cannot affect MemorySize or Timeout.
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
    userPoolArn: auth.userPool.userPoolArn,
  });

  return Template.fromStack(compute).toJSON();
}

function dashboardApiProperties(template: Record<string, any>): Record<string, any> | undefined {
  for (const resource of Object.values(template.Resources ?? {}) as any[]) {
    if (resource.Type !== 'AWS::Lambda::Function') continue;
    if (resource.Properties?.FunctionName === DASHBOARD_API_FUNCTION_NAME) return resource.Properties;
  }
  return undefined;
}

describe('ComputeStack — dashboard_api sizing (p47 R11)', () => {
  let template: Record<string, any>;
  let props: Record<string, any> | undefined;

  beforeAll(() => {
    template = synthesizeCompute();
    props = dashboardApiProperties(template);
  }, 300_000);

  it('renders dashboard_api, so the assertions below resolve something', () => {
    // Without this, a rename makes every check below compare against `undefined`
    // and fail for the wrong reason.
    expect(
      props,
      `no AWS::Lambda::Function named '${DASHBOARD_API_FUNCTION_NAME}' in the Compute template. ` +
        'If it was renamed, update DASHBOARD_API_FUNCTION_NAME here; if it was removed, this ' +
        'whole file and p47 R11 are moot.',
    ).toBeDefined();
  });

  it(`is sized at the measured ${MEASURED_MEMORY_MB} MB (R11.1)`, () => {
    // THE assertion this file exists for. Equality, not a range: R11.1 asked for a
    // value fixed by measurement, and a range would let the value drift back to a
    // guess while still passing.
    expect(
      props!.MemorySize,
      `dashboard_api is ${props!.MemorySize} MB, not the measured ${MEASURED_MEMORY_MB} MB.\n` +
        `D14 measured 1889 real invocations: MaxMemoryUsed peaked at ${OBSERVED_PEAK_MEMORY_MB} MB, ` +
        'with zero OOM kills in the log group\'s history, and the largest marginal per-request ' +
        'Python heap on the post-R7/R8 code is 20.0 MB.\n' +
        'If you are LOWERING it: 256 MB is below the observed peak and would have OOM-killed a ' +
        'real invocation; 384 MB nets only ~0.80x cost, not 0.75x, because vCPU scales with ' +
        'memory and cold-start init (20.4% of billed duration) is CPU-bound and grows 1.33x.\n' +
        'If you are RAISING it to cut latency: for CPU-linear work a memory increase is exactly ' +
        'cost-neutral, and this function\'s expensive tail is a network-bound ThreadPoolExecutor ' +
        'fan-out that does not shorten with CPU at all. 1024 MB needs a 50% duration cut to break ' +
        'even and no measurement supports one.\n' +
        'Either way: take a measurement first and update decision-records/D14-dashboard-api-memory.txt ' +
        'with it. D14 deliberately did NOT produce a duration-vs-memory curve, so if you have one, ' +
        'that is new evidence and this value should move on it.',
    ).toBe(MEASURED_MEMORY_MB);
  });

  it('keeps the configured memory above the observed peak, with headroom', () => {
    // Non-vacuity for the figure above: it ties the constant to the measurement
    // rather than leaving 512 as a bare number that happens to match.
    expect(
      props!.MemorySize,
      `configured memory must exceed the ${OBSERVED_PEAK_MEMORY_MB} MB peak actually observed. ` +
        'A configuration at or below it is an OOM kill on a workload that has already occurred.',
    ).toBeGreaterThan(OBSERVED_PEAK_MEMORY_MB);
  });

  it(`holds the timeout at ${REQUIRED_TIMEOUT_SECONDS} s — not raised as a substitute (R11.3)`, () => {
    // R11.3 is a hard do-NOT and this is where it is enforced. Equality in both
    // directions on purpose: raising it invalidates three derived budgets, and
    // LOWERING it below 22 s would put the ceiling under the 20 s budgets those
    // three modules already spend, turning every slow request into a gateway
    // timeout instead of a reportable partial.
    expect(
      props!.Timeout,
      `dashboard_api timeout is ${props!.Timeout} s, not ${REQUIRED_TIMEOUT_SECONDS} s.\n` +
        'RAISING it: three in-code budgets are derived from this ceiling as "25 s minus 5 s in ' +
        'hand" and all read 20.0 — stats.py:_STATS_TIME_BUDGET_SECONDS, ' +
        'schemas.py:_LISTING_TIME_BUDGET_SECONDS, pipelines_crud.py:_LIST_TIME_BUDGET_SECONDS. ' +
        'None of them move when the timeout does, so partial_counts and enrichment_abandoned would ' +
        'still fire at 20 s and the endpoint would still return at 20 s: you would buy nothing and ' +
        'invalidate the figure they were derived from. LGR-14d6dd3-2 derives the label fan-out ' +
        `deadline against this same value, and ${API_GATEWAY_INTEGRATION_CEILING_SECONDS} s is API ` +
        'Gateway\'s integration ceiling, past which the client gets a 504 with no body — the exact ' +
        'outcome those markers exist to avoid. R11.3 forbids this as a substitute for R7/R8.\n' +
        'LOWERING it: anything under ~22 s puts the ceiling below the 20 s those three modules ' +
        'already spend, which converts reportable partials into gateway timeouts. Move the ' +
        'constants first if that is genuinely wanted.',
    ).toBe(REQUIRED_TIMEOUT_SECONDS);
  });

  it('leaves the timeout below the API Gateway integration ceiling', () => {
    // States the constraint the number was chosen against, so a future edit that
    // reads "25" as arbitrary sees the boundary it is near.
    expect(
      props!.Timeout,
      `the timeout must stay under API Gateway's ${API_GATEWAY_INTEGRATION_CEILING_SECONDS} s ` +
        'integration ceiling; past it the gateway returns 504 with no body and the handler\'s own ' +
        'degradation markers never reach the client.',
    ).toBeLessThan(API_GATEWAY_INTEGRATION_CEILING_SECONDS);
  });

  it('is still the arm64 / Python 3.12 function the measurement was taken on', () => {
    // The measured numbers are architecture- and runtime-specific: the cost
    // arithmetic uses arm64 (Graviton) GB-second pricing, and the 176 MB floor is
    // this runtime's interpreter plus the ADOT collector plus the eagerly imported
    // handler modules. A switch to x86 or a runtime bump invalidates both, so it
    // should land here rather than silently under a value labelled "measured".
    expect(props!.Architectures, 'the D14 cost model uses arm64 GB-second pricing').toEqual(['arm64']);
    expect(
      props!.Runtime,
      'the 176 MB measured floor is python3.12 + ADOT + the eager handler imports',
    ).toBe('python3.12');
  });
});
