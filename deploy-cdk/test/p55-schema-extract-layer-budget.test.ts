// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import { ComputeStack, LAMBDA_CODE_ASSET_EXCLUDE } from '../lib/compute-stack';

/**
 * The Layer_Budget_Assertion for `schema_extract` — p55 R7.1, R7.2, R7.3;
 * design §4.7 (decision W3-D7), risk R5.5. RE-DERIVED for the layer split.
 *
 * WHY THIS EXISTS, AND WHAT THE SPLIT CHANGED ABOUT IT
 * ---------------------------------------------------
 * p55 W3 added `openpyxl==3.1.5` to `shared/runtime_requirements.txt`, installed
 * into `StrandsAgentsLayer`. On `schema_extract` — the one function that also
 * carries `pyarrow-layer` — it consumed **41% of the remaining headroom** against
 * the 262,144,000-byte combined-unzipped Lambda limit, leaving **1,322,903 bytes
 * / 1.26 MiB / 0.5%**. At that margin the arithmetic stopped being a footnote and
 * became something a test had to hold, which is why this file was written.
 *
 * It was also, in practice, a hard block: a version bump to any of strands,
 * pyarrow, mcp, jinja2 or boto3 failed the deploy with `Unzipped size must be
 * smaller than 262144000 bytes`.
 *
 * `schema_extract` now carries **`shared-base-layer` instead of
 * `strands-agents-layer`** — the same manifest filtered through
 * `AGENT_ONLY_LAYER_PACKAGES`, the same `shared/*.py` copy, the same templates,
 * without the agent stack. That returns **124,583,653 bytes / 118.81 MiB** and
 * takes the headroom to **125,906,556 bytes / 120.07 MiB / 48.0%**.
 *
 * THE FILE'S PURPOSE IS UNCHANGED AND SO IS ITS BOUND. This is a re-derivation,
 * not a retirement. A budget with 120 MiB of slack is still a budget: the two
 * layers on this function are the two largest in the stack, PyArrow alone is
 * 123.98 MiB, and the fail-closed half below — *you may not attach a layer nobody
 * has weighed* — is the part that does not care how much room there is. The
 * numbers moved; the reason for asserting them did not.
 *
 * `LGR-0739ab8-1` (template / ratified) states its own `revisit_when` as **"the
 * Layer_Budget changes, or PyArrow stops being needed by `schema_extract`"**.
 * p55 changed the Layer_Budget, which is why this file exists; the split changes
 * it again, which is why every figure below has been re-derived rather than
 * patched. `LGR-bb8073d-1` additionally required that a split "MUST install from
 * `shared/runtime_requirements.txt`" and that this file's budget be
 * "**re-derived by measurement** … in the same change" — both discharged here and
 * in `tests/cdk/stacks/test_shared_base_layer_manifest.py`.
 *
 * WHAT THIS FILE OWNS, AND WHAT IT MUST NOT RESTATE
 * ------------------------------------------------
 * Two ratified assertions own adjacent ground and neither is duplicated or
 * contradicted below:
 *
 *  - `p46-schema-extract-layers.test.ts` (`LGR-0739ab8-1`) owns layer ORDER and
 *    `schema_extract`'s EXACT LIST — now `[sharedBaseLayer, pyarrowLayer]`, no
 *    ADOT, no strands, PyArrow attached nowhere else. Amended by the split in its
 *    own file; the exact list is deliberately NOT restated here.
 *  - `p47-strands-layer-attachment.test.ts` (`LGR-bb8073d-1`) owns the UNIVERSAL
 *    assertion, amended by the split from "every named Compute Lambda carries
 *    `StrandsAgentsLayer`" to "carries exactly one of {strands, shared-base}",
 *    with the non-vacuity floor still at the audited count of 22 and the migrated
 *    set asserted exactly. Also not restated here.
 *
 * *Rejected: adding this bound to `p46-schema-extract-layers.test.ts`.* That
 * file is named in `LGR-0739ab8-1`'s scope and in `LGR-bb8073d-1`'s explicit
 * exclusion list ("keeps sole ownership of layer order and of `schema_extract`'s
 * exact list") precisely so it holds one property. A second, unrelated property
 * there makes a future failure ambiguous between *"the layer list changed"* and
 * *"the layers grew"*, which are different defects with different fixes.
 *
 * So the property here is neither identity nor order. It is: **every layer
 * attached to `schema_extract` has a recorded size measurement, and those
 * measurements plus the function's own code fit under the service limit.** The
 * added value over p46 is the first half — you may not attach a layer nobody has
 * weighed.
 *
 * THE HONEST LIMITATION — RISK R5.5, STATED HERE RATHER THAN HIDDEN
 * ----------------------------------------------------------------
 * A synth-level assertion pins layer IDENTITY and carries a RECORDED
 * measurement. It cannot weigh the built layer without building it, and building
 * it means Docker, a pip resolve and a ~130 MB tree per layer — which is exactly
 * the cost `LGR-1166391-1` and the `aws:cdk:bundling-stacks: []` context below
 * exist to avoid.
 *
 * The consequence, spelled out because it is the whole gap: **this file does NOT
 * detect a dependency added to `shared/runtime_requirements.txt` that grows the
 * layers.** Add a 2 MiB wheel to that manifest and every assertion below still
 * passes; the failure arrives at deploy time as a
 * `CreateFunction`/`UpdateFunctionConfiguration` unzipped-size error, which no
 * test layer below CDK can observe. The split widens the slack that gap eats
 * into, from 1.26 MiB to 120.07 MiB, but it does not close the gap.
 *
 * The mitigation is not this test. It is that the manifest holds a handful of
 * pins and any addition is a one-line diff to a file whose header states its
 * purpose, with the Layer_Budget arithmetic written into that header beside the
 * new pin. A build-time size probe in the bundling command is the fallback, and
 * it is a different and larger mechanism, deliberately not built here.
 *
 * *Considered and rejected: asserting a CEILING on the manifest's pin count*, so
 * any addition fails here and forces a re-measurement. It would close most of
 * R5.5, and design §4.7 chose diff visibility over a test instead — adding the
 * ceiling would contradict a ratified design decision, not extend it. It also
 * duplicates the subject of `test_manifest_covers_layer_dependency_set`, which
 * already asserts the name set as a subset floor.
 *
 * The function-code component IS measured live below, because that one costs
 * nothing: it is git-tracked source read off disk, no Docker involved. That
 * matters more than it sounds — see FUNCTION_CODE_BYTES_P46.
 *
 * WHY THE SYNTHESIZED TEMPLATE, NOT THE SOURCE
 * --------------------------------------------
 * `LGR-1166391-1` (template / ratified) settled this for CDK-shape guards: scan
 * the synthesized template, because that is what CloudFormation hands to the
 * service, and a source-text scan can pass on input that still fails at deploy.
 * It also recorded that the Python `tests/cdk/` layer cannot render a template at
 * all (`aws-cdk-lib` is absent from `pyproject.toml`; every file there asserts on
 * `.ts` text), so vitest is the only suite in which this assertion can exist.
 * Bundling is disabled for the same reason both sibling files disable it: asset
 * content cannot affect a `Layers` entry, and bundling is what exhausts the
 * vitest worker.
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';

const SCHEMA_EXTRACT = 'agentic_data_ingestion_schema_extract';
const STRANDS_LAYER_NAME = `strands-agents-layer-${TEST_ENV}`;
const SHARED_BASE_LAYER_NAME = `shared-base-layer-${TEST_ENV}`;
const PYARROW_LAYER_NAME = `pyarrow-layer-${TEST_ENV}`;

/**
 * The service limit, in bytes.
 *
 * AWS documents "250 MB … including layers, unzipped" without stating the base,
 * and enforces 250 x 1024^2. p46's measurement establishes which reading is
 * live rather than arguing it: the deployed shape sits at 259,897,694 bytes and
 * works, which is impossible under the 250 x 10^6 reading.
 * (`.kiro/specs/p46-avro-orc-schema-extraction/layer-measurement.md`, §"On the
 * units".)
 */
const LAMBDA_UNZIPPED_LIMIT_BYTES = 262_144_000;

/**
 * `strands-agents-layer` as p46 measured it, BEFORE p55's `openpyxl` addition.
 *
 * Apparent byte size of the layer's `python/` tree — the sum of file sizes,
 * which is what AWS counts, not `du` block usage. Cross-checked against the
 * deployed `strands-agents-layer-dev:315` (`Content.CodeSize` 47,217,177 zipped
 * against a 47,305,490 local zip, +0.19%), which is what makes the unzipped
 * figure trustworthy rather than merely plausible.
 */
const STRANDS_LAYER_BYTES_P46 = 129_829_570;

/**
 * What p55 W3 adds to that layer: `openpyxl==3.1.5` plus its one transitive
 * dependency `et_xmlfile` 2.0.0, resolved for arm64-manylinux2014 / py3.12.
 * Pure Python, no compiled extensions.
 *
 * TWO FIGURES EXIST AND THE LARGER ONE IS USED DELIBERATELY.
 *
 *   923,403 bytes (0.88 MiB)  requirements.md R7 — established before the spec,
 *                             stated as the arm64-resolved total. USED HERE.
 *   883,202 bytes (0.84 MiB)  measured locally at task 4.3 (openpyxl 843,554 +
 *                             et_xmlfile 39,648), excluding `__pycache__`.
 *
 * `__pycache__` exclusion is the right comparable because the layer bundling
 * deletes `__pycache__` and `*.pyc` after install (`compute-stack.ts:199-200`;
 * the manifest header cites `:198-200`, whose `:198` is the sibling `tests`
 * removal).
 *
 * Task 4.3 also verified both `dist-info/WHEEL` files declare
 * `Tag: py3-none-any` and `Root-Is-Purelib: true`, so the arm64 bytes should
 * equal the local bytes — but it could NOT download the arm64 wheel to check
 * directly (no `pip` in the project `.venv`). So 883,202 is a direct local
 * measurement whose transfer to arm64 is an inference, and 923,403 is a figure
 * whose raw measurement is not in the tree. Neither is a measurement of a built
 * layer.
 *
 * A budget must not rest on the optimistic number, and the larger figure yields
 * the SMALLER headroom, so using it makes this bound independent of which
 * provenance is better: 923,403 gives 1.26 MiB of recorded headroom, 883,202
 * would give 1.30 MiB. Re-measuring can only ever move this in the safe
 * direction.
 */
const OPENPYXL_BYTES_P55 = 923_403;

/** The strands layer as p55 leaves it. No longer attached to `schema_extract`. */
const STRANDS_LAYER_BYTES = STRANDS_LAYER_BYTES_P46 + OPENPYXL_BYTES_P55;

/**
 * What the agent stack costs — `AGENT_ONLY_LAYER_PACKAGES` plus its transitive
 * closure — and therefore what the split returns.
 *
 * MEASURED, not inferred, and measured as a DIFFERENCE so the recorded strands
 * figure's provenance carries over to the base figure derived from it below.
 * Both sides installed with `--python-version 3.12 --python-platform
 * aarch64-manylinux2014 --only-binary :all:` (the same resolution constraints the
 * bundling passes to `pip`), then `tests`, `__pycache__` and `*.pyc` removed as
 * the bundling does, then `shared/*.py`, `shared/prompt_templates/` and
 * `pyspark_templates/` copied in as the bundling does. Apparent file sizes summed
 * — not `du` block usage — because apparent size is what AWS counts.
 *
 *   full manifest    130,466,884 B  124.42 MiB   7,295 files
 *   filtered           5,883,231 B    5.61 MiB     646 files
 *   ------------------------------------------------------------
 *   difference       124,583,653 B  118.81 MiB
 *
 * THE CROSS-CHECK THAT MAKES THIS TRUSTWORTHY. The local full-manifest figure
 * (130,466,884) sits **0.22% below** `STRANDS_LAYER_BYTES` (130,752,973), which is
 * p46's Docker-built measurement cross-checked against the deployed
 * `strands-agents-layer-dev:315`. Two independent measurements of the same layer
 * agreeing to 0.22% is what licenses using the local difference against the
 * recorded total. A separate confirmation of the method: `openpyxl` +
 * `et_xmlfile` measured **843,554 + 39,648 = 883,202** here, byte-identical to
 * p55 task 4.3's local figure.
 *
 * Neither side is a measurement of a built LAYER — R5.5 again. Nothing below
 * pretends otherwise.
 */
const AGENT_STACK_BYTES = 124_583_653;

/**
 * `shared-base-layer`, the figure the budget uses.
 *
 * TWO FIGURES EXIST AND THE LARGER ONE IS USED DELIBERATELY, the same discipline
 * this file already applies to `OPENPYXL_BYTES_P55`:
 *
 *   6,169,320 bytes (5.88 MiB)  recorded strands total minus the measured agent
 *                               difference. USED HERE.
 *   5,883,231 bytes (5.61 MiB)  measured directly, locally, method above.
 *
 * The derived figure is 286,089 bytes larger, and that gap is exactly the 0.22%
 * by which the local full-manifest measurement undershot the deployed-cross-
 * checked one. Deriving rather than measuring therefore inherits the conservative
 * side of that discrepancy instead of assuming it away, and it keeps the whole
 * budget expressed against one authority for the common bytes. A direct
 * re-measurement can only ever move this in the safe direction.
 *
 * By construction `AGENT_STACK_BYTES + BASE_LAYER_BYTES === STRANDS_LAYER_BYTES`,
 * which is asserted below so the three cannot drift apart.
 */
const BASE_LAYER_BYTES = STRANDS_LAYER_BYTES - AGENT_STACK_BYTES;

/**
 * `pyarrow-layer`, p46's measurement. Essentially one wheel: pyarrow accounts
 * for 129,809,384 of this. The deployed layer resolves pyarrow **20.0.0**, not
 * the 24.0.0 the p46 spec text cited — `--platform manylinux2014_aarch64` with
 * `--only-binary=:all:` caps it there, because newer wheels are
 * `manylinux_2_28`.
 */
const PYARROW_LAYER_BYTES_P46 = 130_007_672;

/**
 * Every layer whose unzipped size has actually been measured, keyed by the
 * `LayerName` the template renders.
 *
 * This map is the fail-closed half of the assertion: a layer attached to
 * `schema_extract` that is absent from here fails, because its contribution to
 * the total is unknown and an unknown contribution cannot be shown to fit. The
 * ADOT layer is deliberately NOT pre-recorded with an approximate figure — only
 * measured numbers belong in a budget table, and the failure message names ADOT
 * as the most plausible case with what it costs.
 *
 * `strands-agents-layer` stays in the table although `schema_extract` no longer
 * carries it. Removing it would make a re-attachment fail the *unmeasured-layer*
 * assertion instead of the *over-budget* one — the wrong diagnosis for the actual
 * problem, which is 124 MB of bytes and not an unknown quantity.
 */
const RECORDED_LAYER_BYTES: Record<string, number> = {
  [STRANDS_LAYER_NAME]: STRANDS_LAYER_BYTES,
  [SHARED_BASE_LAYER_NAME]: BASE_LAYER_BYTES,
  [PYARROW_LAYER_NAME]: PYARROW_LAYER_BYTES_P46,
};

/**
 * The function's own code as p46 recorded it — and the reason the function-code
 * component is measured live below instead of being carried as a constant.
 *
 * p46 task 1 ran BEFORE p46's implementation, so 60,452 bytes describes the
 * `schema_extract` directory as it was before the Avro and ORC parsers were
 * written into `lambda_function.py`. By p46's own merge commit `0739ab88` the
 * same five git-tracked files summed to 105,696 bytes — the recorded headroom
 * was already 45,244 bytes optimistic at the moment it was recorded. It has
 * grown again since.
 *
 * That is a measurement going stale inside one commit, which is the exact
 * failure mode R5.5 warns about for the layer figures. It cannot be fixed for
 * the layers without a build, but it can be fixed here for free, so it is.
 * 60,452 is kept only to reproduce R7.3's stated headroom.
 */
const FUNCTION_CODE_BYTES_P46 = 60_452;

/**
 * Recorded totals using p46's function-code figure — i.e. the arithmetic behind
 * the number R7.3 asks this test to state, RE-DERIVED after the layer split.
 *
 * WHAT IT WAS, IMMEDIATELY BEFORE THE SPLIT. Kept because the split's whole
 * justification is this table, and a reader who cannot see the "before" cannot
 * judge whether the change was worth making:
 *
 *   strands   129,829,570  +  openpyxl 923,403  =  130,752,973
 *   pyarrow                                        130,007,672
 *   function code (p46)                                 60,452
 *   ---------------------------------------------------------
 *   total                                          260,821,097   248.74 MiB
 *   limit                                          262,144,000   250.00 MiB
 *   HEADROOM                                         1,322,903     1.26 MiB   0.5%
 *
 * Before p55 the headroom was 2,246,306 bytes / 2.14 MiB (`LGR-0739ab8-1`), so
 * `openpyxl` consumed 41% of it. That ratio is why R7 is a requirement rather
 * than a nicety, and it is still asserted below as history.
 *
 * WHAT IT IS NOW. `schema_extract` carries `shared-base-layer` in place of
 * `strands-agents-layer`:
 *
 *   shared-base (derived)                            6,169,320     5.88 MiB
 *   pyarrow                                        130,007,672   123.98 MiB
 *   function code (p46)                                 60,452     0.06 MiB
 *   ---------------------------------------------------------
 *   total                                          136,237,444   129.93 MiB
 *   limit                                          262,144,000   250.00 MiB
 *   HEADROOM                                       125,906,556   120.07 MiB  48.0%
 *
 * The gain is 125,906,556 − 1,322,903 = **124,583,653 bytes**, which is
 * `AGENT_STACK_BYTES` exactly, as it must be — the only thing that changed is
 * which of two otherwise-identical layers is attached. That identity is asserted
 * below rather than asserted in prose here.
 *
 * These are pinned as constants and checked against the arithmetic below so the
 * prose in this header cannot drift away from the numbers the file computes.
 *
 * ONE CORRECTION, RECORDED RATHER THAN QUIETLY APPLIED — carried forward from the
 * pre-split revision because it is still true and still growing. 120.07 MiB uses
 * p46's 60,452-byte function-code component. The live measurement is **158,274
 * bytes across 5 files** (it was 145,149 when this file landed, and 60,452 when
 * p46 recorded it), which puts the real headroom at **125,808,734 B / 119.98
 * MiB**. Both are reported: 120.07 MiB is the recorded arithmetic, 119.98 MiB is
 * what the tree measures. The BOUND below uses the live figure, because a budget
 * should use the number it can actually observe; the recorded figure is pinned
 * separately so the arithmetic stays checkable. If the base layer turns out to
 * cost its directly-measured 5,883,231 bytes rather than the derived 6,169,320,
 * add 286,089 bytes back to each.
 */
const RECORDED_HEADROOM_BYTES = 125_906_556;
const RECORDED_HEADROOM_MIB = '120.07';

/**
 * The pre-split headroom, kept so the split's value is stated as a number this
 * file checks rather than as a claim in a comment.
 */
const HEADROOM_BEFORE_SPLIT_BYTES = 1_322_903;

/** Bytes per MiB, spelled out because the units are the thing that goes wrong. */
const MIB = 1024 * 1024;

function mib(bytes: number): string {
  return (bytes / MIB).toFixed(2);
}

/**
 * True when a Lambda code asset would exclude a directory entry of this name.
 *
 * `LAMBDA_CODE_ASSET_EXCLUDE` is imported from `compute-stack.ts` rather than
 * transcribed, so the measurement below cannot drift from what CDK actually
 * ships. Throws on a pattern shape it cannot honour, because silently
 * under-matching a new exclude entry would overstate the deployed size and make
 * this file fail for a reason that is not a budget breach.
 */
function isExcludedAssetEntry(name: string): boolean {
  return LAMBDA_CODE_ASSET_EXCLUDE.some((pattern) => {
    const stars = pattern.split('*').length - 1;
    if (stars === 0) return name === pattern;
    if (stars === 1 && pattern.startsWith('*')) return name.endsWith(pattern.slice(1));
    if (stars === 1 && pattern.endsWith('*')) return name.startsWith(pattern.slice(0, -1));
    throw new Error(
      `p55-schema-extract-layer-budget: LAMBDA_CODE_ASSET_EXCLUDE gained the pattern ` +
        `'${pattern}', which this file's matcher does not handle. Extend the matcher — ` +
        'under-matching would overstate the measured function-code size.',
    );
  });
}

/**
 * Apparent byte size of a Lambda code asset directory, applying the same
 * excludes CDK applies. Recursive because the exclude list is interpreted with
 * `IgnoreMode.GIT`, which matches at any depth.
 */
function assetByteSize(dir: string): { bytes: number; files: string[] } {
  const files: string[] = [];
  let bytes = 0;

  const walk = (absolute: string, relative: string): void => {
    for (const entry of fs.readdirSync(absolute, { withFileTypes: true })) {
      if (isExcludedAssetEntry(entry.name)) continue;
      const childAbsolute = path.join(absolute, entry.name);
      const childRelative = relative ? `${relative}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        walk(childAbsolute, childRelative);
        continue;
      }
      if (!entry.isFile()) continue;
      bytes += fs.statSync(childAbsolute).size;
      files.push(childRelative);
    }
  };

  walk(dir, '');
  return { bytes, files: files.sort() };
}

interface RenderedFunction {
  logicalId: string;
  functionName: string;
  layers: unknown[];
}

function synthesizeCompute(): Record<string, any> {
  // Bundling disabled — see the header note and both sibling files. Asset
  // content cannot affect a `Layers` entry, and bundling is what exhausts the
  // vitest worker.
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

/** `LayerName` of every `AWS::Lambda::LayerVersion`, keyed by logical id. */
function layerNamesByLogicalId(template: Record<string, any>): Record<string, string> {
  const names: Record<string, string> = {};
  for (const [logicalId, resource] of Object.entries(template.Resources ?? {}) as [string, any][]) {
    if (resource.Type !== 'AWS::Lambda::LayerVersion') continue;
    const layerName = resource.Properties?.LayerName;
    if (typeof layerName === 'string') names[logicalId] = layerName;
  }
  return names;
}

/** Every `AWS::Lambda::Function` that declares a `FunctionName`. */
function namedFunctions(template: Record<string, any>): RenderedFunction[] {
  const functions: RenderedFunction[] = [];
  for (const [logicalId, resource] of Object.entries(template.Resources ?? {}) as [string, any][]) {
    if (resource.Type !== 'AWS::Lambda::Function') continue;
    const functionName = resource.Properties?.FunctionName;
    if (typeof functionName !== 'string') continue;
    functions.push({ logicalId, functionName, layers: resource.Properties?.Layers ?? [] });
  }
  return functions;
}

/**
 * Human-readable identity of a rendered `Layers` entry.
 *
 * A `{ Ref: … }` to a LayerVersion in this stack resolves to its `LayerName`.
 * Anything else — a literal ARN from `fromLayerVersionArn`, an `Fn::Join`, an
 * `Fn::ImportValue` — is returned stringified, which keeps it OUT of
 * `RECORDED_LAYER_BYTES` and therefore fails the sizing assertion rather than
 * being skipped. Failing on an entry whose size cannot be looked up is the
 * point.
 */
function layerIdentity(entry: unknown, names: Record<string, string>): string {
  if (entry && typeof entry === 'object' && !Array.isArray(entry)) {
    const ref = (entry as Record<string, unknown>).Ref;
    if (typeof ref === 'string' && names[ref]) return names[ref];
  }
  return JSON.stringify(entry);
}

describe('ComputeStack — schema_extract Layer_Budget (p55 R7)', () => {
  let template: Record<string, any>;
  let schemaExtract: RenderedFunction | undefined;
  let layerNames: Record<string, string>;
  let attachedIdentities: string[];
  let functionCode: { bytes: number; files: string[] };

  beforeAll(() => {
    template = synthesizeCompute();
    layerNames = layerNamesByLogicalId(template);
    schemaExtract = namedFunctions(template).find((f) => f.functionName === SCHEMA_EXTRACT);
    attachedIdentities = (schemaExtract?.layers ?? []).map((entry) => layerIdentity(entry, layerNames));
    functionCode = assetByteSize(
      path.join(__dirname, '..', '..', 'lambda', SCHEMA_EXTRACT),
    );
  }, 300_000);

  it('resolves schema_extract and at least one attached layer, so nothing below is vacuous', () => {
    // Without this a rename or a refactor makes every assertion below compare
    // against an empty list and pass while asserting nothing. The exact list and
    // its order are p46-schema-extract-layers.test.ts's property, not this
    // file's — this is only a floor.
    expect(schemaExtract, `no Lambda named '${SCHEMA_EXTRACT}' in the Compute template`).toBeDefined();
    expect(
      attachedIdentities.length,
      `${SCHEMA_EXTRACT} renders no Layers entry. If the attachment was genuinely removed, ` +
        'p46-schema-extract-layers.test.ts is the file that owns that fact and will say so ' +
        'more precisely; this budget assertion has simply stopped covering anything.',
    ).toBeGreaterThanOrEqual(1);
    expect(
      functionCode.files.length,
      `no files measured under lambda/${SCHEMA_EXTRACT}/ — the function-code component of the ` +
        'budget below would be understated as zero',
    ).toBeGreaterThanOrEqual(1);
  });

  it('every layer attached to schema_extract has a recorded size measurement (R7.1)', () => {
    // The fail-closed half, and the part p46 does not cover. p46 asserts WHICH
    // layers are attached; this asserts that whatever is attached has been
    // WEIGHED. A layer with no measurement cannot be shown to fit, so it fails
    // here rather than at deploy.
    const unmeasured = attachedIdentities.filter((id) => !(id in RECORDED_LAYER_BYTES)).sort();

    expect(
      unmeasured,
      `these layers are attached to ${SCHEMA_EXTRACT} with no recorded unzipped size: ` +
        `${unmeasured.join(', ')}.\n` +
        'A layer whose size nobody has measured cannot be shown to fit under the 250 MiB ' +
        `combined-unzipped limit, and only ${RECORDED_HEADROOM_MIB} MiB is free.\n` +
        'If this is the ADOT layer (aws-otel-python-arm64): it costs roughly 50 MB. It used to ' +
        'be excluded from this function because "combined ADOT+Strands+PyArrow exceeds 250MB ' +
        'limit"; after the layer split it would FIT, and it is now excluded as a deliberate ' +
        'hold — see p46-schema-extract-layers.test.ts. If you are adding it on purpose, measure ' +
        'it and record the figure here rather than approximating it.\n' +
        'If it is a new layer: measure its python/ tree (sum of file sizes, not du) the way ' +
        'p46/layer-measurement.md did, add it to RECORDED_LAYER_BYTES, and re-derive the ' +
        'headroom below in the same change.',
    ).toEqual([]);
  });

  it('recorded layers plus measured function code fit under the 250 MiB limit (R7.1, R7.2)', () => {
    // THE bound. Layer sizes are recorded measurements (R5.5: they cannot be
    // weighed at synth time); the function-code component is measured live off
    // disk, because that costs nothing and p46's constant went stale inside its
    // own commit.
    const layerBytes = attachedIdentities.reduce((sum, id) => sum + (RECORDED_LAYER_BYTES[id] ?? 0), 0);
    const total = layerBytes + functionCode.bytes;
    const liveHeadroom = LAMBDA_UNZIPPED_LIMIT_BYTES - total;

    expect(
      total,
      `${SCHEMA_EXTRACT} exceeds the 250 MiB combined-unzipped Lambda limit.\n` +
        `  layers (recorded)   ${layerBytes.toLocaleString()} B  ${mib(layerBytes)} MiB  ` +
        `[${attachedIdentities.join(', ')}]\n` +
        `  function code (live) ${functionCode.bytes.toLocaleString()} B  ${mib(functionCode.bytes)} MiB  ` +
        `(${functionCode.files.length} files)\n` +
        `  total                ${total.toLocaleString()} B  ${mib(total)} MiB\n` +
        `  limit                ${LAMBDA_UNZIPPED_LIMIT_BYTES.toLocaleString()} B  ` +
        `${mib(LAMBDA_UNZIPPED_LIMIT_BYTES)} MiB\n` +
        `  over by              ${(-liveHeadroom).toLocaleString()} B  ${mib(-liveHeadroom)} MiB\n` +
        'This is a CreateFunction / UpdateFunctionConfiguration refusal at deploy, not a ' +
        'slowdown. The most likely cause now is that the strands layer has been re-attached in ' +
        'place of, or alongside, shared-base — that is 124,583,653 bytes and it is the whole ' +
        'reason the split happened. Otherwise: drop a dependency from ' +
        'shared/runtime_requirements.txt; migrate more of the manifest out of the base layer ' +
        'via AGENT_ONLY_LAYER_PACKAGES; or stop needing PyArrow here, which is the other arm of ' +
        "LGR-0739ab8-1's revisit condition and would free 123.98 MiB.",
    ).toBeLessThanOrEqual(LAMBDA_UNZIPPED_LIMIT_BYTES);
  });

  it('states the measured headroom, and the stated figure matches the arithmetic (R7.3)', () => {
    // R7.3 asks the bound to carry the measured headroom as the reason for the
    // number. Asserting it rather than only writing it in a comment is what
    // stops the header's prose and the constants drifting apart — the failure
    // mode this whole file exists to make loud.
    //
    // RE-DERIVED for the split: the first term is BASE_LAYER_BYTES, not
    // STRANDS_LAYER_BYTES. Everything else is unchanged.
    const recordedTotal =
      BASE_LAYER_BYTES + PYARROW_LAYER_BYTES_P46 + FUNCTION_CODE_BYTES_P46;

    expect(
      LAMBDA_UNZIPPED_LIMIT_BYTES - recordedTotal,
      'the recorded headroom no longer follows from the recorded measurements. Re-derive ' +
        'RECORDED_HEADROOM_BYTES and the table in this file\'s header together, and say which ' +
        'measurement moved.',
    ).toBe(RECORDED_HEADROOM_BYTES);
    expect(mib(RECORDED_HEADROOM_BYTES)).toBe(RECORDED_HEADROOM_MIB);
  });

  it('the split accounts for exactly the agent stack, and nothing else moved', () => {
    // The identity that makes the re-derivation checkable rather than asserted.
    // shared-base and strands differ ONLY by AGENT_ONLY_LAYER_PACKAGES and their
    // transitive closure — same manifest, same shared/*.py copy, same templates —
    // so the three figures must close. If they stop closing, one of them was
    // edited without re-measuring the others.
    expect(
      BASE_LAYER_BYTES + AGENT_STACK_BYTES,
      'shared-base + the agent stack no longer equals the strands layer. The two layers are the ' +
        'same bundling with one filtered manifest, so these three figures are two independent ' +
        'measurements and a difference — re-measure, do not patch one constant.',
    ).toBe(STRANDS_LAYER_BYTES);

    // And the headroom moved by exactly that amount, which is the split's whole
    // claim stated as arithmetic rather than as prose.
    expect(
      RECORDED_HEADROOM_BYTES - HEADROOM_BEFORE_SPLIT_BYTES,
      'the headroom gain no longer equals the agent stack, so something other than the layer ' +
        'swap has changed this budget. Say what.',
    ).toBe(AGENT_STACK_BYTES);
    expect(mib(HEADROOM_BEFORE_SPLIT_BYTES)).toBe('1.26');
  });

  it('still records what openpyxl cost, because that is why this file exists (history)', () => {
    // Kept, not deleted. p55's `openpyxl` addition taking 41% of a 2.14 MiB
    // margin is the event that made a one-line manifest edit into a
    // deploy-blocking change, and it is the reason a budget assertion exists at
    // all. The split moved the margin; it did not make the history wrong, and a
    // future reader asking "why is there a test for this" needs it.
    //
    // Measured against the PRE-SPLIT headroom, which is what those percentages
    // were ever about.
    const headroomBeforeP55 = HEADROOM_BEFORE_SPLIT_BYTES + OPENPYXL_BYTES_P55;
    expect(headroomBeforeP55, 'LGR-0739ab8-1 records 2,246,306 B / 2.14 MiB before p55').toBe(2_246_306);
    expect(Math.round((OPENPYXL_BYTES_P55 / headroomBeforeP55) * 100)).toBe(41);

    // Post-split the same wheel would be 0.7% of the margin. Stated so the
    // contrast is a number rather than an impression.
    expect(Math.round((OPENPYXL_BYTES_P55 / RECORDED_HEADROOM_BYTES) * 1000) / 10).toBeLessThan(1);
  });

  it('the live measurement is no rosier than the recorded one (R5.5, from the one side it can check)', () => {
    // The function-code component is the only part of this budget that can be
    // re-measured without Docker, and it is also the part that already drifted:
    // p46 recorded 60,452 B before writing p46's own Avro/ORC parsers, and the
    // directory has grown every spec since. So the live total is expected to be
    // LARGER than the recorded total, and this assertion pins the direction
    // rather than the amount — an unexpected SHRINK means files stopped being
    // measured, not that the budget improved.
    //
    // Nothing here weakens R5.5: the layers are still recorded figures, and a
    // wheel added to shared/runtime_requirements.txt is still invisible to every
    // assertion in this file.
    expect(
      functionCode.bytes,
      `measured ${functionCode.bytes.toLocaleString()} B of function code under lambda/` +
        `${SCHEMA_EXTRACT}/, below p46's recorded ${FUNCTION_CODE_BYTES_P46.toLocaleString()} B. ` +
        'A shrink this large usually means the walk stopped seeing files (an exclude pattern ' +
        'widened, or the directory moved), which would understate the budget.',
    ).toBeGreaterThanOrEqual(FUNCTION_CODE_BYTES_P46);
  });
});
