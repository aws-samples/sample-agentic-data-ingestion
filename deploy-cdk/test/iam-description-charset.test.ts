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
import { AgentCoreStack } from '../lib/agentcore-stack';
import { ComputeStack } from '../lib/compute-stack';
import { OrchestrationStack } from '../lib/orchestration-stack';
import { EdgeWafStack } from '../lib/edge-waf-stack';
import { HostingStack } from '../lib/hosting-stack';
import { TestFixturesStack } from '../lib/test-fixtures-stack';

/**
 * Every IAM description in every stack must satisfy IAM's charset constraint.
 *
 * WHY THIS EXISTS
 * ---------------
 * The `cdk deploy --all` that first shipped `VerificationSessionRole` (added by
 * `LGR-ad78946-4`) failed on Foundation with
 *
 *   VerificationSessionRole/Resource (AWS::IAM::Role)
 *   Resource handler returned message: "1 validation error detected: Value at
 *   'description' failed to satisfy constraint: Member must satisfy regular
 *   expression pattern: [\u0009\u000A\u000D\u0020-\u007E\u00A1-\u00FF]*"
 *
 * because the role description contained an em dash (U+2014), which sits above
 * IAM's allowed range. The stack rolled back cleanly, but the round trip cost a
 * full deploy cycle to discover a defect that is fully determined at synth.
 *
 * `LGR-b2f79ef-7` (template / ratified) is the governing precedent for pinning
 * a CDK shape with an assertion: it added one for a grant that was correct at
 * the time, on the ground that the failure it protects against "surfaces as an
 * AccessDenied that no test layer below CDK can observe". The same argument
 * holds for a different service error. Here it is not a judgement call but a
 * fact about the tree: `aws-cdk-lib` is not a Python dependency
 * (absent from `pyproject.toml`), so no unit, contract or integration test can
 * render a CloudFormation template at all, and nothing below this layer can see
 * a malformed description.
 *
 * WHY THE SYNTHESIZED TEMPLATE, NOT THE SOURCE
 * --------------------------------------------
 * The template is what CloudFormation hands to IAM. A description assembled
 * from concatenated literals (which the failing one was, across three source
 * lines) or from a template string could pass a source-text scan and still
 * fail at deploy. Scanning `Template.fromStack(...).toJSON()` removes the gap
 * between what a test reads and what the service validates.
 *
 * WHY THIS SUITE AND NOT `tests/cdk/` (PYTHON)
 * --------------------------------------------
 * This guard must read the SYNTHESIZED template, and vitest is the only suite
 * in this repository that can produce one.
 *
 * The Python `tests/cdk/` layer does not render templates: all 21 files there
 * open `deploy-cdk/lib/*.ts` (or `bin/*.ts`) and assert on the TEXT. The single
 * grep hit for "cdk synth" anywhere under `tests/` is a comment in
 * `test_agentcore_stack_synth.py:9-10` saying exactly that — full synth
 * validation "requires the CDK CLI and Node.js environment — use `npx cdk
 * synth` manually". Giving it a template would mean shelling out to `npx cdk
 * synth`, which needs a concrete `CDK_DEFAULT_ACCOUNT` (`lib/resolve-env.ts:20-27`
 * throws on anything that is not 12 digits) and runs Docker asset bundling —
 * minutes, onto a suite that currently finishes in ~0.5 s. Reading a prebuilt
 * artifact is not an option either: `cdk.out/` is gitignored (root
 * `.gitignore:35` and `:37`, and `deploy-cdk/.gitignore:3`).
 *
 * The recorded reason no vitest test synthesizes Compute or AgentCore
 * (`foundation-iam.test.ts:200-202`, `hosting-auth.test.ts:271-274`,
 * `hosting-auth.test.ts:326-333`) is memory during ASSET BUNDLING — those notes
 * name the cause themselves: "20+ Lambdas with Code.fromAsset". Disabling
 * bundling with the `aws:cdk:bundling-stacks: []` App context that
 * `foundation-iam.test.ts:80` already uses removes it. MEASURED, not assumed:
 * all nine stacks synthesize here in ~2.9 s with heapUsed going 54 -> 101 MB,
 * comfortably inside the default worker limit. For scale, the same app under a
 * real bundling `npx cdk synth` pip-installs the Strands dependency tree and
 * Docker-builds the PyArrow and Strands layers.
 *
 * Bundling affects asset content and asset hashes, never a `Description`
 * property. Confirmed by comparing this render of
 * `VerificationSessionRoleDFD70013` against a full bundling `npx cdk synth
 * AgenticDataIngestion-Foundation-dev`: byte-identical descriptions.
 *
 * THE TWO CONSTRAINTS ARE NOT THE SAME
 * ------------------------------------
 * Confirmed against the CloudFormation resource reference rather than assumed
 * equal:
 *
 *   AWS::IAM::Role.Description        Pattern:
 *                                     [\u0009\u000A\u000D\u0020-\u007E\u00A1-\u00FF]*
 *                                     Maximum: 1000
 *   AWS::IAM::ManagedPolicy.Description
 *                                     NO Pattern documented
 *                                     Maximum: 1000
 *                                     (also: immutable once assigned)
 *
 * So the two genuinely differ, and the difference is encoded rather than
 * flattened. Managed policies get the length limit plus the *IAM-family* string
 * charset — `[\u0009\u000A\u000D\u0020-\u00FF]*`, the pattern the same resource
 * reference documents for the adjacent free-text parameter `PolicyDocument`.
 * That is deliberately LOOSER than the role pattern: it does not exclude
 * U+007F-U+00A0, because no AWS document says IAM rejects those in a policy
 * description and this guard should not invent a constraint. It is still tight
 * enough to reject the characters that actually caused the outage (em dash, en
 * dash, curly quotes, ellipsis), which is a defensible house rule for prose
 * that ends up in an IAM API call.
 *
 * Role and ManagedPolicy are the complete set: no other `AWS::IAM::*` resource
 * type has a `Description` property (`AWS::IAM::Policy`, `::Group`, `::User`,
 * `::InstanceProfile` do not declare one).
 *
 * Note that `U+00A1`-`U+00FF` is ALLOWED by both. `é`, `ñ` and `£` are legal in
 * an IAM description, so this must not degrade into an ASCII-only check.
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';

/** The charset IAM documents for `CreateRole`'s `Description`. */
const ROLE_DESCRIPTION_CHARSET = /^[\u0009\u000A\u000D\u0020-\u007E\u00A1-\u00FF]*$/;
/**
 * The charset IAM documents for its free-text string parameters. Applied to
 * managed-policy descriptions, for which AWS documents no charset of their own.
 */
const IAM_STRING_CHARSET = /^[\u0009\u000A\u000D\u0020-\u00FF]*$/;
/** Both fields are capped at 1000 characters. */
const MAX_DESCRIPTION_LENGTH = 1000;

type IamDescribedType = 'AWS::IAM::Role' | 'AWS::IAM::ManagedPolicy';

interface Constraint {
  charset: RegExp;
  /** Human-readable provenance, quoted in failure messages. */
  source: string;
}

const CONSTRAINTS: Record<IamDescribedType, Constraint> = {
  'AWS::IAM::Role': {
    charset: ROLE_DESCRIPTION_CHARSET,
    source: "IAM CreateRole 'Description' pattern",
  },
  'AWS::IAM::ManagedPolicy': {
    charset: IAM_STRING_CHARSET,
    source: "IAM free-text string pattern (CreatePolicy documents no charset for 'Description')",
  },
};

const DESCRIBED_TYPES = Object.keys(CONSTRAINTS) as IamDescribedType[];

interface FoundDescription {
  stack: string;
  logicalId: string;
  type: IamDescribedType;
  description: string;
}

interface UnresolvedDescription {
  stack: string;
  logicalId: string;
  type: IamDescribedType;
  /** The non-string value found, so a failure names the intrinsic. */
  raw: unknown;
}

interface ScanResult {
  found: FoundDescription[];
  unresolved: UnresolvedDescription[];
}

/**
 * Collect every `Description` on an IAM resource type that has one, out of a
 * set of rendered templates keyed by stack name.
 */
function scanDescriptions(templates: Record<string, Record<string, any>>): ScanResult {
  const found: FoundDescription[] = [];
  const unresolved: UnresolvedDescription[] = [];
  for (const [stack, json] of Object.entries(templates)) {
    for (const [logicalId, resource] of Object.entries(json.Resources ?? {}) as [string, any][]) {
      if (!DESCRIBED_TYPES.includes(resource.Type)) continue;
      const description = resource.Properties?.Description;
      if (description === undefined || description === null) continue;
      if (typeof description === 'string') {
        found.push({ stack, logicalId, type: resource.Type, description });
      } else {
        unresolved.push({ stack, logicalId, type: resource.Type, raw: description });
      }
    }
  }
  return { found, unresolved };
}

/**
 * Offending characters in `value`, with codepoints, for a readable failure.
 * `charset` is applied one character at a time, so the caller's whole-string
 * anchored pattern is reused rather than reimplemented.
 */
function offendingCharacters(value: string, charset: RegExp): string[] {
  const bad = new Set<string>();
  for (const ch of value) {
    if (!charset.test(ch)) {
      bad.add(
        `${JSON.stringify(ch)} (U+${ch.codePointAt(0)!.toString(16).toUpperCase().padStart(4, '0')})`,
      );
    }
  }
  return [...bad];
}

/** Build one App containing every stack the deployed app defines. */
function synthesizeAllStacks(): Record<string, Record<string, any>> {
  // Bundling disabled: see the header note. Asset content does not affect any
  // resource property, and bundling is what exhausts the vitest worker.
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
  const agentcore = new AgentCoreStack(app, `AgenticDataIngestion-AgentCore-${TEST_ENV}`, {
    env,
    environment: TEST_ENV,
    foundation,
  });
  const compute = new ComputeStack(app, `AgenticDataIngestion-Compute-${TEST_ENV}`, {
    env,
    environment: TEST_ENV,
    projectName: 'agentic-data-ingestion',
    foundation,
    data,
    userPoolArn: auth.userPool.userPoolArn,
  });
  const orchestration = new OrchestrationStack(
    app,
    `AgenticDataIngestion-Orchestration-${TEST_ENV}`,
    { env, environment: TEST_ENV, compute, data, foundation },
  );
  const edgeWaf = new EdgeWafStack(app, `AgenticDataIngestion-EdgeWaf-${TEST_ENV}`, {
    env: { account: TEST_ACCOUNT, region: 'us-east-1' },
    environment: TEST_ENV,
    crossRegionReferences: true,
  });
  const hosting = new HostingStack(app, `AgenticDataIngestion-Hosting-${TEST_ENV}`, {
    env,
    environment: TEST_ENV,
    crossRegionReferences: true,
    restApiHost: `${compute.api.restApiId}.execute-api.${TEST_REGION}.amazonaws.com`,
    userPoolId: auth.userPool.userPoolId,
    userPoolArn: auth.userPool.userPoolArn,
    hostedUiDomain: auth.userPoolDomain.baseUrl(),
    wsUrl: compute.wsApiEndpoint,
    originVerifySecretValue: compute.originVerifySecret?.secretValue.unsafeUnwrap(),
    cloudFrontWebAclArn: edgeWaf.webAclArn,
  });
  // Context-gated in bin/, but it declares IAM and can carry a description, so
  // it is covered here rather than left to a deploy with the flag set.
  const fixtures = new TestFixturesStack(app, `AgenticDataIngestion-TestFixtures-${TEST_ENV}`, {
    env,
    environment: TEST_ENV,
  });

  const stacks: cdk.Stack[] = [
    foundation,
    data,
    auth,
    agentcore,
    compute,
    orchestration,
    edgeWaf,
    hosting,
    fixtures,
  ];
  const templates: Record<string, Record<string, any>> = {};
  for (const stack of stacks) {
    templates[stack.stackName] = Template.fromStack(stack).toJSON();
  }
  return templates;
}

/**
 * Every stack class the app entry point instantiates. Used to fail loudly if a
 * stack is added to `bin/` and not to this guard, which would silently shrink
 * the scan.
 */
function stackClassesInAppEntry(): string[] {
  const entry = path.join(__dirname, '..', 'bin', 'agentic_data_ingestion.ts');
  const source = fs.readFileSync(entry, 'utf8');
  const names = new Set<string>();
  for (const match of source.matchAll(/new\s+(\w+Stack)\s*\(\s*app\s*,/g)) {
    names.add(match[1]);
  }
  return [...names].sort();
}

/**
 * The stack classes this guard synthesizes. Kept as a literal list so the
 * comparison against `bin/` is a real comparison and not a tautology.
 */
const COVERED_STACK_CLASSES = [
  'AgentCoreStack',
  'AuthStack',
  'ComputeStack',
  'DataStack',
  'EdgeWafStack',
  'FoundationStack',
  'HostingStack',
  'OrchestrationStack',
  'TestFixturesStack',
].sort();

/**
 * Non-vacuity floor. Measured inventory across all nine synthesized stacks —
 * 55 resources of any type carry a Description, of which exactly 5 are IAM, all
 * in Foundation:
 *
 *   VerificationSessionRoleDFD70013        Role           the P42 role, full prose
 *   DynamoDBPolicyF7DB0DAD                 ManagedPolicy  ''  (CDK default)
 *   BedrockPolicy9EAD7B51                  ManagedPolicy  ''  (CDK default)
 *   AgentCorePolicyC988FEBD                ManagedPolicy  ''  (CDK default)
 *   InfrastructureOverflowPolicy10C18BEF9  ManagedPolicy  'Part of the policies
 *                                                         for ...\/Infrastructure'
 *                                                         (CDK-generated split)
 *
 * No other stack renders a described IAM resource.
 *
 * These are floors, not equalities: adding a described role must not fail the
 * guard, but a synth shape change that stops producing descriptions must,
 * because a scan that finds nothing passes trivially.
 */
const MIN_DESCRIBED_IAM_RESOURCES = 5;
const MIN_DESCRIBED_ROLES = 1;
const MIN_DESCRIBED_MANAGED_POLICIES = 4;

describe('IAM descriptions satisfy IAM charset constraints (all stacks, synthesized)', () => {
  let templates: Record<string, Record<string, any>>;
  let scan: ScanResult;

  beforeAll(() => {
    templates = synthesizeAllStacks();
    scan = scanDescriptions(templates);
  }, 300_000);

  it('every IAM description matches the charset its resource type documents', () => {
    const violations = scan.found
      .filter((d) => !CONSTRAINTS[d.type].charset.test(d.description))
      .map(
        (d) =>
          `${d.stack} :: ${d.logicalId} (${d.type}) violates ${CONSTRAINTS[d.type].source}: ` +
          `${offendingCharacters(d.description, CONSTRAINTS[d.type].charset).join(', ')} in ${JSON.stringify(d.description)}`,
      );
    expect(violations, violations.join('\n')).toEqual([]);
  });

  it('every IAM description is within the 1000-character limit', () => {
    const tooLong = scan.found
      .filter((d) => d.description.length > MAX_DESCRIPTION_LENGTH)
      .map((d) => `${d.stack} :: ${d.logicalId} (${d.type}) is ${d.description.length} characters`);
    expect(tooLong, tooLong.join('\n')).toEqual([]);
  });

  it('no IAM description is an unresolved intrinsic the charset check cannot see', () => {
    // A Fn::Join / Ref description would slip past the scan silently. Not
    // hypothetical: three NON-IAM descriptions in these same templates are
    // already Fn::Join — the S3 auto-delete handler in Foundation and Hosting,
    // and both TestFixtures secrets. None is an IAM type today. If an IAM one is
    // ever needed, assert its resolved parts here rather than deleting this.
    const unresolved = scan.unresolved.map(
      (d) => `${d.stack} :: ${d.logicalId} (${d.type}) has a non-string Description: ${JSON.stringify(d.raw)}`,
    );
    expect(unresolved, unresolved.join('\n')).toEqual([]);
  });

  it('the scan is non-vacuous: it found the described IAM resources it expects', () => {
    const roles = scan.found.filter((d) => d.type === 'AWS::IAM::Role');
    const policies = scan.found.filter((d) => d.type === 'AWS::IAM::ManagedPolicy');
    expect(
      scan.found.length,
      `only ${scan.found.length} described IAM resources found across ${Object.keys(templates).length} stacks — ` +
        'if the synth shape changed so descriptions are no longer rendered, this guard is disabled, not passing',
    ).toBeGreaterThanOrEqual(MIN_DESCRIBED_IAM_RESOURCES);
    expect(roles.length).toBeGreaterThanOrEqual(MIN_DESCRIBED_ROLES);
    expect(policies.length).toBeGreaterThanOrEqual(MIN_DESCRIBED_MANAGED_POLICIES);
  });

  it('covers every stack the app entry point instantiates', () => {
    expect(
      COVERED_STACK_CLASSES,
      'bin/agentic_data_ingestion.ts instantiates a stack this guard does not synthesize — ' +
        'add it to synthesizeAllStacks() and COVERED_STACK_CLASSES',
    ).toEqual(stackClassesInAppEntry());
    expect(Object.keys(templates).length).toBe(COVERED_STACK_CLASSES.length);
  });

  it('the specific role that failed the deploy is scanned and clean', () => {
    // Named explicitly so a rename or removal of the role surfaces here rather
    // than quietly reducing coverage. Deliberately NOT pinned to the exact
    // sentence: rewording this prose is legitimate, shipping U+2014 in it is
    // not, and the charset is the real constraint.
    const role = scan.found.find((d) => d.logicalId.startsWith('VerificationSessionRole'));
    expect(role, 'VerificationSessionRole has no rendered Description').toBeDefined();
    expect(role!.description.length).toBeGreaterThan(0);
    expect(role!.description).toMatch(ROLE_DESCRIPTION_CHARSET);
    expect(role!.description, 'the em dash that failed the deploy is back').not.toContain('\u2014');
  });
});

describe('the charset guard bites', () => {
  it('rejects an em dash in a role description rendered from a real stack', () => {
    // End-to-end proof: the same scanner, over a real synthesized template,
    // must report a violation for the exact character that failed the deploy.
    const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
    const stack = new cdk.Stack(app, 'CharsetProbe', {
      env: { account: TEST_ACCOUNT, region: TEST_REGION },
    });
    new cdk.aws_iam.Role(stack, 'EmDashRole', {
      assumedBy: new cdk.aws_iam.ServicePrincipal('glue.amazonaws.com'),
      description: 'log writes only \u2014 never a write',
    });
    new cdk.aws_iam.ManagedPolicy(stack, 'EmDashPolicy', {
      description: 'grants nothing \u2014 placeholder',
      statements: [
        new cdk.aws_iam.PolicyStatement({ actions: ['s3:GetObject'], resources: ['*'] }),
      ],
    });

    const probe = scanDescriptions({ CharsetProbe: Template.fromStack(stack).toJSON() });
    const violations = probe.found.filter(
      (d) => !CONSTRAINTS[d.type].charset.test(d.description),
    );

    expect(violations.map((v) => v.logicalId).sort()).toEqual(
      expect.arrayContaining([expect.stringContaining('EmDashPolicy'), expect.stringContaining('EmDashRole')]),
    );
    expect(offendingCharacters(violations[0].description, CONSTRAINTS[violations[0].type].charset)).toEqual([
      '"—" (U+2014)',
    ]);
  });

  it('rejects the other non-ASCII punctuation this codebase carries in prose', () => {
    const rejected = [
      ['en dash', '\u2013'],
      ['left curly single quote', '\u2018'],
      ['right curly single quote', '\u2019'],
      ['left curly double quote', '\u201C'],
      ['right curly double quote', '\u201D'],
      ['ellipsis', '\u2026'],
      ['box drawing', '\u2500'],
      ['rightwards arrow', '\u2192'],
      ['almost equal to', '\u2248'],
    ] as const;
    for (const [name, ch] of rejected) {
      expect(ROLE_DESCRIPTION_CHARSET.test(`a ${ch} b`), `role charset must reject ${name}`).toBe(
        false,
      );
      expect(IAM_STRING_CHARSET.test(`a ${ch} b`), `IAM charset must reject ${name}`).toBe(false);
    }
  });

  it('accepts Latin-1 Supplement, which IAM explicitly allows', () => {
    // U+00A1-U+00FF is inside the allowed range. An ASCII-only assertion would
    // be wrong here and would fail on legitimate input later.
    for (const value of ['café', 'mañana', '£10 budget', '¡hola!', 'Ünicode', 'ÿ']) {
      expect(ROLE_DESCRIPTION_CHARSET.test(value), `role charset must accept ${value}`).toBe(true);
      expect(IAM_STRING_CHARSET.test(value), `IAM charset must accept ${value}`).toBe(true);
    }
    // Section sign (U+00A7) appears in this codebase's comments and is allowed.
    expect(ROLE_DESCRIPTION_CHARSET.test('see design §4.6')).toBe(true);
  });

  it('accepts tab, LF and CR, and the printable ASCII range', () => {
    expect(ROLE_DESCRIPTION_CHARSET.test('a\tb\nc\rd')).toBe(true);
    expect(ROLE_DESCRIPTION_CHARSET.test(' !"#$%&\'()*+,-./0123456789:;<=>?@AZ[\\]^_`az{|}~')).toBe(
      true,
    );
    expect(ROLE_DESCRIPTION_CHARSET.test('')).toBe(true);
  });

  it('applies the role charset more tightly than the IAM string charset', () => {
    // The documented difference between the two, encoded rather than assumed
    // away: U+007F-U+00A0 is outside CreateRole's pattern but inside IAM's
    // general string pattern.
    for (const ch of ['\u007F', '\u0085', '\u00A0']) {
      expect(ROLE_DESCRIPTION_CHARSET.test(ch)).toBe(false);
      expect(IAM_STRING_CHARSET.test(ch)).toBe(true);
    }
  });
});
