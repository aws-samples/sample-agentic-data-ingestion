// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';

/**
 * CDK assertion tests pinning the Glue ARN naming forms in Foundation IAM
 * policies (publication-cleanup Req 9.8).
 *
 * Glue databases/tables are snake_case (`agentic_data_ingestion*` — Glue
 * forbids hyphens there), but runtime-created jobs/crawlers/connections are
 * kebab-case (`agentic-data-ingestion-*`). Pre-rename, the single prefix
 * (the internal codename) matched both styles; post-rename the forms diverge, and the
 * policies must carry BOTH. Regression here caused a real outage:
 * `glue:GetJob` AccessDenied on `agentic-data-ingestion-job-*` from the
 * AgentCore runtime role (2026-07-17, fresh-account validation).
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';

// The kebab-case resource ARNs every Glue-scoped statement must include.
const KEBAB_JOB = `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:job/agentic-data-ingestion-*`;
const KEBAB_CRAWLER = `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:crawler/agentic-data-ingestion-*`;
const KEBAB_CONNECTION = `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:connection/agentic-data-ingestion-*`;
// The snake_case forms that must be preserved for databases/tables.
const SNAKE_DATABASE = `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:database/agentic_data_ingestion*`;
// Account-wide connection read: glue_connection-mode targets (and the
// TestFixtures agentglue-test-fixtures-* connections) reference PRE-EXISTING
// Glue Connections whose names are unknown at deploy time.
const ACCOUNT_WIDE_CONNECTION = `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:connection/*`;
// Secret prefixes that no AgentCore runtime may read any longer: T-19 removed the
// job builder's job-time credential lookup, and R-02 did not carry the grant
// to any runtime role. Formerly needed to resolve a connection's
// adi:secret_arn (app prefix + legacy P18 test-fixtures prefix).
const APP_SECRET_PREFIX = `arn:aws:secretsmanager:${TEST_REGION}:${TEST_ACCOUNT}:secret:agentic-data-ingestion/*`;
const TEST_FIXTURES_SECRET_PREFIX = `arn:aws:secretsmanager:${TEST_REGION}:${TEST_ACCOUNT}:secret:agentglue/test-fixtures/*`;

type PolicyStatement = {
  Action: string | string[];
  Resource: string | string[];
  Effect: string;
};

/** Collect every IAM policy statement in the template (inline + managed). */
function allStatements(json: Record<string, any>): PolicyStatement[] {
  const statements: PolicyStatement[] = [];
  for (const resource of Object.values(json.Resources ?? {}) as any[]) {
    if (resource.Type === 'AWS::IAM::Policy' || resource.Type === 'AWS::IAM::ManagedPolicy') {
      statements.push(...(resource.Properties?.PolicyDocument?.Statement ?? []));
    }
    if (resource.Type === 'AWS::IAM::Role') {
      for (const inline of resource.Properties?.Policies ?? []) {
        statements.push(...(inline.PolicyDocument?.Statement ?? []));
      }
    }
  }
  return statements;
}

/** Statements whose actions include Glue job-level access (glue-star or glue:Get / StartJobRun). */
function glueJobStatements(statements: PolicyStatement[]): PolicyStatement[] {
  return statements.filter((s) => {
    const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
    const resources = Array.isArray(s.Resource) ? s.Resource : [s.Resource];
    const touchesGlueJobs = resources.some(
      (r) => typeof r === 'string' && r.includes(':job/agentic'),
    );
    return (
      touchesGlueJobs &&
      actions.some((a) => typeof a === 'string' && a.startsWith('glue:'))
    );
  });
}

describe('FoundationStack — Glue ARN naming forms (publication-cleanup Req 9.8)', () => {
  let json: Record<string, any>;

  beforeAll(() => {
    const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
    const stack = new FoundationStack(app, `AgenticDataIngestion-Foundation-${TEST_ENV}`, {
      env: { account: TEST_ACCOUNT, region: TEST_REGION },
      projectName: 'agentic-data-ingestion',
      environment: TEST_ENV,
    });
    json = Template.fromStack(stack).toJSON();
  });

  it('every Glue statement scoping job ARNs includes the kebab-case job form', () => {
    const statements = glueJobStatements(allStatements(json));
    expect(statements.length).toBeGreaterThanOrEqual(3); // agentcore policy, glueAdmin consumers, runtime role, job-status statement
    for (const s of statements) {
      const resources = Array.isArray(s.Resource) ? s.Resource : [s.Resource];
      expect(resources, `statement with actions ${JSON.stringify(s.Action)}`).toContain(KEBAB_JOB);
    }
  });

  it('the broad glue:* statements include kebab crawler and connection forms', () => {
    const statements = allStatements(json).filter((s) => {
      const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
      return actions.includes('glue:*');
    });
    expect(statements.length).toBeGreaterThanOrEqual(1);
    for (const s of statements) {
      const resources = Array.isArray(s.Resource) ? s.Resource : [s.Resource];
      expect(resources).toContain(KEBAB_CRAWLER);
      expect(resources).toContain(KEBAB_CONNECTION);
    }
  });

  it('the AgentCore runtime role can read arbitrarily-named Glue connections (glue_connection mode + test fixtures)', () => {
    // create_glue_pipeline (_resolve_jdbc_target_credentials) reads a
    // PRE-EXISTING Glue Connection whose name does NOT follow the app's
    // agentic-data-ingestion-* convention — a customer-managed connection or
    // the TestFixtures agentglue-test-fixtures-* connections — at job-creation
    // time. The names are unknown at deploy time, so the runtime role needs
    // account-wide READ (GetConnection/GetConnections/GetTags) on
    // connection/*. Regression here => glue:GetConnection AccessDenied for
    // glue_connection-mode targets. The broad glue:* statement above stays
    // scoped to agentic-data-ingestion-* (writes are NOT broadened).
    const statements = allStatements(json).filter((s) => {
      const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
      return actions.includes('glue:GetConnection');
    });
    expect(statements.length).toBeGreaterThanOrEqual(1);
    const readsAnyConnection = statements.some((s) => {
      const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
      const resources = Array.isArray(s.Resource) ? s.Resource : [s.Resource];
      return (
        actions.includes('glue:GetConnection') &&
        actions.includes('glue:GetConnections') &&
        actions.includes('glue:GetTags') &&
        resources.includes(ACCOUNT_WIDE_CONNECTION)
      );
    });
    expect(
      readsAnyConnection,
      'runtime role must read arbitrarily-named Glue connections (+ tags) via connection/*',
    ).toBe(true);
  });

  it('no AgentCore runtime role can read the connection credential secrets (T-19, R-02)', () => {
    const runtimeRoleKeys = Object.entries(json.Resources ?? {})
      .filter(([, r]: [string, any]) => r.Type === 'AWS::IAM::Role' && /-agentcore-[a-z-]+-role$/.test(r.Properties?.RoleName ?? ''))
      // The retired shared role grants nothing and is kept one release for its export.
      .filter(([, r]: [string, any]) => !r.Properties.RoleName.endsWith('-agentcore-runtime-role'))
      .map(([k]) => k);
    expect(runtimeRoleKeys.length, 'nine agent roles and one MCP role').toBe(10);
    for (const key of runtimeRoleKeys) {
      const reads = statementsForRole(json, key).filter((s) => actionsOf(s).includes('secretsmanager:GetSecretValue'));
      expect(reads, `${key} reads a secret`).toEqual([]);
    }
    // The prefixes stay pinned on the Lambda role that still needs them.
    const secretReads = allStatements(json).filter((s) => actionsOf(s).includes('secretsmanager:GetSecretValue'));
    expect(secretReads.some((s) => resourcesOf(s).includes(APP_SECRET_PREFIX))).toBe(true);
    expect(secretReads.some((s) => resourcesOf(s).includes(TEST_FIXTURES_SECRET_PREFIX))).toBe(true);
  });

  it('every database ARN uses the snake_case form (Glue forbids hyphens in db names)', () => {
    // Some statements scope narrower snake prefixes (e.g. the JDBC discovery
    // databases), so assert the naming FORM, not one exact pattern: no
    // database/table ARN may use the kebab prefix, and the generic snake
    // pattern must appear in at least the broad glue statements.
    const dbArns = allStatements(json)
      .flatMap((s) => (Array.isArray(s.Resource) ? s.Resource : [s.Resource]))
      .filter((r): r is string => typeof r === 'string' && r.includes(':glue:') && (r.includes(':database/') || r.includes(':table/')) && r.includes('agentic')); // skip wildcard-any read statements (database/*)
    expect(dbArns.length).toBeGreaterThanOrEqual(4);
    for (const arn of dbArns) {
      // No database/table ARN should use the kebab prefix (Glue forbids hyphens)
      expect(arn, 'database/table ARNs must not use kebab-case prefix').not.toContain('agentic-data-ingestion');
    }
    // At least some ARNs must use the snake_case prefix (broad wildcards like :database/* are OK)
    const snakeArns = dbArns.filter((a) => a.includes('agentic_data_ingestion'));
    expect(snakeArns.length).toBeGreaterThanOrEqual(2);
    const withGeneric = allStatements(json).filter((s) =>
      (Array.isArray(s.Resource) ? s.Resource : [s.Resource]).includes(SNAKE_DATABASE),
    );
    expect(withGeneric.length).toBeGreaterThanOrEqual(2); // agentcore policy + glue:* statements
  });
});

// ─────────────────────────────────────────────────────────────────────
// P42 real-Spark verification — the IAM surface (task 2.3)
//
// Requirements 6.1, 6.2, 6.3, 6.5, 6.6, 6.7.
//
// WHY THESE ASSERT AGAINST THE SYNTHESIZED TEMPLATE. The Python counterpart
// (`tests/cdk/stacks/test_verification_session_role_iam.py`) reads
// foundation-stack.ts as text, which is the only way to see a comment (the
// naming fence) or a deliberate source shape. It cannot see a permission that
// arrives some other way — a `bucket.grantRead(role)`, an attached managed
// policy, a grant added by a construct. R6.3 is an assertion about the role's
// WHOLE authority, so it needs the effective policy, and that is what
// CloudFormation renders here.
//
// The Foundation stack synthesizes cheaply (the four suites above already do
// it). ComputeStack does not — a full ComputeStack synth exceeds the vitest
// worker's memory — so keep P42 CDK assertions on Foundation in this file.
//
// Pinning an IAM shape with a test rather than trusting it follows
// LGR-fb2310b-1 and LGR-b2f79ef-7, both template/ratified. b2f79ef-7 states the
// mechanism: a later tightening breaks a read "with an AccessDenied that no
// test layer below CDK can observe".
// ─────────────────────────────────────────────────────────────────────

const VERIFICATION_ROLE_NAME = 'AgenticDataIngestion-VerificationSession-Role';
const GLUE_PRINCIPAL = 'glue.amazonaws.com';
/** foundation-stack.ts: the verifier's AgentCore runtime role (R-02 split). */
const RUNTIME_ROLE_NAME = `agentic-data-ingestion-${TEST_ENV}-agentcore-test-code-role`;
/** Every AgentCore runtime role name: `${projectName}-${environment}-agentcore-<runtime>-role`. */
const RUNTIME_ROLE_NAME_PATTERN = /^agentic-data-ingestion-dev-agentcore-[a-z-]+-role$/;

/**
 * R6.3 — the complete set of actions the verification session role may hold.
 *
 * Read design §4.6's Decision before adding to this set. The code a
 * verification session executes is AI-generated and unreviewed by a human at
 * that point (the code-approval gate is downstream of TestCode), so a write
 * action here lets a verification step alter the data it is verifying against.
 *
 * An allowlist rather than a denylist of known-bad actions: a denylist passes
 * the first time someone adds an action nobody thought to forbid.
 */
const VERIFICATION_ROLE_ALLOWED_ACTIONS = new Set([
  's3:GetObject',
  's3:ListBucket',
  'glue:GetDatabase',
  'glue:GetTable',
  'glue:GetPartitions',
  'logs:CreateLogStream',
  'logs:PutLogEvents',
]);

/** R6.5 — session ids are minted per verification run, hence the wildcard. */
const SESSION_RESOURCE = `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:session/*`;
const SNAKE_JOB = `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:job/agentic_data_ingestion*`;
const SNAKE_CRAWLER = `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:crawler/agentic_data_ingestion*`;
const SNAKE_CONNECTION = `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:connection/agentic_data_ingestion*`;
/** The pre-existing UNCONDITIONED PassRole that must not be what satisfies R6.7. */
const BROAD_PASSROLE_RESOURCE = `arn:aws:iam::${TEST_ACCOUNT}:role/AgenticDataIngestion-*`;

type NamedResource = { key: string; resource: any };

/** The template resource of the given type whose RoleName/property matches. */
function findResource(
  json: Record<string, any>,
  type: string,
  predicate: (props: any) => boolean,
): NamedResource {
  const entries = Object.entries(json.Resources ?? {}) as [string, any][];
  const hits = entries.filter(([, r]) => r.Type === type && predicate(r.Properties ?? {}));
  expect(hits.length, `expected exactly one ${type} matching the predicate`).toBe(1);
  return { key: hits[0][0], resource: hits[0][1] };
}

/**
 * Every statement attached to one role, from every attachment route.
 *
 * Covers inline `Policies` on the role and standalone `AWS::IAM::Policy`
 * resources that name it in `Roles`. Managed policies are asserted absent
 * separately — an attached managed policy's statements would not be visible
 * here, which is exactly why that assertion exists.
 */
function statementsForRole(json: Record<string, any>, roleKey: string): PolicyStatement[] {
  const statements: PolicyStatement[] = [];
  const role = json.Resources[roleKey];
  for (const inline of role.Properties?.Policies ?? []) {
    statements.push(...(inline.PolicyDocument?.Statement ?? []));
  }
  for (const resource of Object.values(json.Resources ?? {}) as any[]) {
    if (resource.Type !== 'AWS::IAM::Policy') continue;
    const roles = resource.Properties?.Roles ?? [];
    if (roles.some((r: any) => r?.Ref === roleKey)) {
      statements.push(...(resource.Properties?.PolicyDocument?.Statement ?? []));
    }
  }
  return statements;
}

function actionsOf(s: PolicyStatement): string[] {
  return Array.isArray(s.Action) ? s.Action : [s.Action];
}

function resourcesOf(s: PolicyStatement): any[] {
  return Array.isArray(s.Resource) ? s.Resource : [s.Resource];
}

describe('FoundationStack — real-Spark verification IAM surface (P42 R6)', () => {
  let json: Record<string, any>;
  let roleKey: string;
  let role: any;
  let processedBucketKey: string;
  let sourceBucketKey: string;

  beforeAll(() => {
    const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
    const stack = new FoundationStack(app, `AgenticDataIngestion-Foundation-${TEST_ENV}-P42`, {
      env: { account: TEST_ACCOUNT, region: TEST_REGION },
      projectName: 'agentic-data-ingestion',
      environment: TEST_ENV,
    });
    json = Template.fromStack(stack).toJSON();
    const found = findResource(
      json,
      'AWS::IAM::Role',
      (props) => props.RoleName === VERIFICATION_ROLE_NAME,
    );
    roleKey = found.key;
    role = found.resource;
    // Resolved from the template rather than guessed: the logical id carries a
    // CDK-generated suffix, so a hardcoded name would silently stop matching.
    processedBucketKey = findResource(
      json,
      'AWS::S3::Bucket',
      (props) => props.BucketName === `adi-processed-files-${TEST_ACCOUNT}-${TEST_REGION}`,
    ).key;
    sourceBucketKey = findResource(
      json,
      'AWS::S3::Bucket',
      (props) => props.BucketName === `adi-source-files-${TEST_ACCOUNT}-${TEST_REGION}`,
    ).key;
  });

  // ── R6.1: the role exists and trusts Glue ──────────────────────────

  it('the verification session role exists and trusts the Glue service principal (R6.1)', () => {
    // Without this trust, CreateSession fails on the trust policy rather than
    // on a permission — a different error at the same point in the run.
    const principals = (role.Properties.AssumeRolePolicyDocument?.Statement ?? []).flatMap(
      (s: any) => {
        const service = s.Principal?.Service;
        return Array.isArray(service) ? service : [service];
      },
    );
    expect(principals, 'the verification role must be assumable by Glue').toContain(GLUE_PRINCIPAL);
    expect(
      principals.filter((p: string) => p !== GLUE_PRINCIPAL),
      'only Glue may assume this role — it holds read access to the source and processed buckets',
    ).toEqual([]);
  });

  // ── R6.2 / R6.3: exactly the reads verification needs, and nothing else ──

  it('the verification session role grants the three reads R6.2 requires', () => {
    // Iterating the allowlist is deliberate, not a shortcut: design §4.6 grants
    // exactly those actions, so R6.2 (each is present) and R6.3 (nothing else
    // is) are the two directions of one set equality. Each group is only useful
    // whole — GetObject without ListBucket cannot enumerate a prefix,
    // PutLogEvents without CreateLogStream has nowhere to write. An absent grant
    // surfaces as an AccessDenied inside a Glue statement, which reads as a
    // failed transform rather than as a failed harness.
    const granted = new Set(statementsForRole(json, roleKey).flatMap(actionsOf));
    for (const action of VERIFICATION_ROLE_ALLOWED_ACTIONS) {
      expect(granted, `R6.2 requires ${action} on ${VERIFICATION_ROLE_NAME}`).toContain(action);
    }
  });

  it('the allowlist itself stays read-shaped (R6.3 cannot be widened away)', () => {
    // Without this, R6.3 is one edit from vacuous: add the action to the role,
    // add it to the allowlist, green. So the allowlist is itself constrained to
    // the three services R6.2 names, read-shaped verbs on s3 and glue, and on
    // logs only the two stream-write actions R6.2 explicitly permits.
    //
    // A widened allowlist does also trip the R6.2 test above (it starts
    // demanding the new action), but that failure reads as "a grant is missing".
    // This one says what actually happened. Mirrors
    // test_the_allowlist_itself_stays_read_shaped in the Python counterpart.
    //
    // Asserts on the constant, not the template — it fails at edit time.
    const readVerb = /^(Get|List|Describe|Head|BatchGet)/;
    const logWrites = new Set(['logs:CreateLogStream', 'logs:PutLogEvents']);

    for (const action of VERIFICATION_ROLE_ALLOWED_ACTIONS) {
      const [service, verb] = action.split(':');
      expect(
        ['s3', 'glue', 'logs'],
        `${action} is outside the services R6.2 names — R6.3 forbids reaching a ` +
          `DynamoDB item, a secret or a table bucket at all`,
      ).toContain(service);
      if (service === 'logs') {
        expect(
          logWrites.has(action),
          `${action} is a logs action outside the two R6.2 permits — no CreateLogGroup, no reads`,
        ).toBe(true);
      } else {
        expect(
          readVerb.test(verb),
          `${action} is not read-shaped. R6.3 grants no action that creates, modifies ` +
            `or deletes a pipeline target, a catalog object, a table bucket, a DynamoDB ` +
            `item or a secret — adding this to the allowlist is the widening R6.3 exists ` +
            `to prevent`,
        ).toBe(true);
      }
    }
  });

  it('the verification session role grants NOTHING outside the read/log allowlist (R6.3)', () => {
    // The load-bearing assertion. Against the synthesized template rather than
    // source text, so a grant added by any route — a construct's grant* helper,
    // a resource policy, a second AWS::IAM::Policy naming this role — is caught.
    const statements = statementsForRole(json, roleKey);
    expect(statements.length, 'no statements were found for the role — assertion vacuous').toBeGreaterThan(0);

    const extra = [...new Set(statements.flatMap(actionsOf))].filter(
      (a) => !VERIFICATION_ROLE_ALLOWED_ACTIONS.has(a),
    );
    expect(
      extra,
      `${VERIFICATION_ROLE_NAME} grants action(s) outside the allowlist. Read design ` +
        `§4.6's Decision before widening: this role executes unreviewed AI-generated ` +
        `PySpark, so a write here lets a verification step corrupt what it verifies`,
    ).toEqual([]);

    for (const s of statements) {
      expect(s.Effect, 'a Deny statement changes what the allowlist means').toBe('Allow');
      expect(
        actionsOf(s).filter((a) => a.includes('*')),
        'no wildcard action: it makes the allowlist unenforceable and future ' +
          'additions to the service action set arrive silently',
      ).toEqual([]);
    }
  });

  it('no managed policy is attached to the verification session role (R6.3)', () => {
    // A managed policy's statements are not in this template, so they would be
    // invisible to the allowlist above. LGR-b46a5bb-2 rejected exactly this
    // shortcut for agentDispatchRole: attaching AgentCorePolicy is one line
    // instead of two statements and grants s3tables:* plus
    // bedrock-agentcore:InvokeAgentRuntime on Resource '*' along the way.
    expect(
      role.Properties.ManagedPolicyArns ?? [],
      'an attached managed policy would carry actions R6.3 cannot see',
    ).toEqual([]);
  });

  it('the verification role reaches the source-files and processed-files buckets, and no other (R6.3)', () => {
    // AMENDED (p44). This previously required EVERY S3 resource to reference the
    // processed bucket, which pinned a live defect: on the S3-upload path the
    // sample URI names the SOURCE bucket — file_validation writes
    // original_s3_uri from the upload event's own bucket (:632), schema_extract
    // carries it to json_s3_uri (:904 -> :960 -> :1021), and Step Functions maps
    // that to source_data_uri (orchestration-stack.ts:594, via source_sample_uri, same value here). The grant denied it,
    // and the denial surfaced inside the Glue session as a failed transform.
    //
    // Widened by exactly one bucket and asserted in both directions: each of the
    // two must be reachable (a permissive test would go green again if the
    // source-bucket resources were deleted), and nothing else may be.
    //
    // Scope, not just action, is still the point: a read-only role pointed at
    // every adi-* bucket also reads the target schemas and the generated code.
    const s3Resources = statementsForRole(json, roleKey)
      .filter((s) => actionsOf(s).some((a) => a.startsWith('s3:')))
      .flatMap(resourcesOf);
    expect(s3Resources.length, 'no S3 resources found on the role').toBeGreaterThan(0);

    // Rendered as Fn::GetAtt / Fn::Join against a bucket, so match on the
    // serialized form rather than by string equality.
    const serialized = s3Resources.map((r) => JSON.stringify(r));
    const permitted: Array<[string, string]> = [
      [sourceBucketKey, 'source-files (the S3-upload path\u2019s source_data_uri names it)'],
      [processedBucketKey, 'processed-files (the legacy converted-JSON path\u2019s json_s3_uri)'],
    ];

    for (const [key, why] of permitted) {
      expect(
        serialized.some((r) => r.includes(key)),
        `the verification role reaches no resource under ${why}. Both buckets are ` +
          `required, not merely permitted — a sample read denied here surfaces as a ` +
          `failed transform rather than a failed harness`,
      ).toBe(true);
    }

    for (const r of serialized) {
      expect(
        permitted.some(([key]) => r.includes(key)),
        `an S3 resource on the verification role references neither the source-files ` +
          `nor the processed-files bucket: ${r}. Every entry must be traceable to a URI ` +
          `a verification sample can be read from`,
      ).toBe(true);
    }
  });

  // ── R6.5 / R6.6: the caller-side Glue statement ────────────────────

  it('the verifier\'s AgentCore runtime role reaches the whole Glue session lifecycle (R6.5)', () => {
    // Since R-02 the test_code runtime role enumerates the six session APIs on
    // session/*, as codeTestingRole does, instead of reaching sessions through
    // the shared role's glue:* statement. A missing one is an AccessDenied
    // partway through a verification, after a billable session exists.
    const { key } = findResource(json, 'AWS::IAM::Role', (props) => props.RoleName === RUNTIME_ROLE_NAME);
    const sessionStatements = statementsForRole(json, key).filter((s) => resourcesOf(s).includes(SESSION_RESOURCE));
    const granted = new Set(sessionStatements.flatMap(actionsOf));
    // glue:TagResource because create_session passes Tags (verified AccessDenied
    // on a deployment, 2026-10-08).
    for (const action of ['glue:CreateSession', 'glue:TagResource', 'glue:GetSession', 'glue:RunStatement', 'glue:GetStatement', 'glue:StopSession', 'glue:DeleteSession']) {
      expect(granted.has(action), `${RUNTIME_ROLE_NAME} lacks ${action} on ${SESSION_RESOURCE}`).toBe(true);
    }
    expect([...granted].filter((a) => a.endsWith(':*')), 'enumerate, do not wildcard (LGR-b46a5bb-2)').toEqual([]);
  });

  it('both Glue naming forms survive on every glue:* statement (R6.6)', () => {
    // A fence, not a preference: the kebab form was added after a verified
    // glue:GetJob AccessDenied from this role in fresh-account validation
    // (2026-07-17), and the snake form is required because Glue forbids hyphens
    // in database and table names. Adding session/* to this resource list is
    // exactly the kind of edit that drops a neighbouring entry, so R6.6 is
    // asserted next to R6.5 rather than assumed.
    const glueStar = allStatements(json).filter((s) => actionsOf(s).includes('glue:*'));
    expect(glueStar.length).toBeGreaterThanOrEqual(1);
    for (const s of glueStar) {
      const resources = resourcesOf(s);
      for (const [snake, kebab] of [
        [SNAKE_JOB, KEBAB_JOB],
        [SNAKE_CRAWLER, KEBAB_CRAWLER],
        [SNAKE_CONNECTION, KEBAB_CONNECTION],
      ]) {
        expect(resources, `snake_case form ${snake} missing (NAMING FENCE, p31 C3)`).toContain(snake);
        expect(resources, `kebab-case form ${kebab} missing (NAMING FENCE, p31 C3)`).toContain(kebab);
      }
    }
  });

  // ── R6.7: the conditioned PassRole ─────────────────────────────────

  it('the iam:PassRole grant on the verification role is conditioned on iam:PassedToService (R6.7)', () => {
    // Selected by RESOURCE, not by action, and that is the whole assertion. An
    // unconditioned iam:PassRole on role/AgenticDataIngestion-* already exists
    // (foundation-stack.ts:1042-1045, alongside iam:PutRolePolicy and
    // iam:AttachRolePolicy) and matches this role by name, so "some statement
    // grants PassRole and can reach it" was already true before this spec and
    // pins nothing.
    const passRoleOnThisRole = allStatements(json).filter(
      (s) =>
        actionsOf(s).includes('iam:PassRole') &&
        resourcesOf(s).some((r) => JSON.stringify(r).includes(roleKey)),
    );
    expect(
      passRoleOnThisRole.length,
      'no iam:PassRole statement names the verification role by ARN reference',
    ).toBeGreaterThanOrEqual(1);

    for (const s of passRoleOnThisRole) {
      expect(
        (s as any).Condition?.StringEquals?.['iam:PassedToService'],
        'the PassRole grant on the verification role must be conditioned on ' +
          'iam:PassedToService — otherwise any service the runtime role can call ' +
          'could be handed a role that reads the processed bucket',
      ).toBe(GLUE_PRINCIPAL);
    }
  });

  // RETIRED DELIBERATELY (threat model T-03): 'the broad unconditioned
  // iam:PassRole would not satisfy R6.7'. It asserted that the unconditioned
  // PassRole on role/AgenticDataIngestion-* still existed, and said to retire it
  // once that grant was tightened. It now is: the runtime role's IAM statement
  // is split and narrowed to `-ExecutionRole`, with PassRole conditioned on
  // Glue. The R6.7 discriminator above still selects by resource; the tests
  // below replace the tripwire with the property it was standing in for.

  it('no iam:PassRole in Foundation is unconditioned, and none reaches the verification role by wildcard (T-03)', () => {
    const passRole = allStatements(json).filter((s) => actionsOf(s).includes('iam:PassRole'));
    expect(passRole.length).toBeGreaterThanOrEqual(4);
    for (const s of passRole) {
      expect(
        (s as any).Condition?.StringEquals?.['iam:PassedToService'],
        `unconditioned iam:PassRole on ${JSON.stringify(s.Resource)}: any service the role ` +
          'can call could be handed the passed role',
      ).toBe(GLUE_PRINCIPAL);
      expect(
        resourcesOf(s).includes(BROAD_PASSROLE_RESOURCE),
        `iam:PassRole on ${BROAD_PASSROLE_RESOURCE} reaches AgenticDataIngestion-VerificationSession-Role by name`,
      ).toBe(false);
    }
  });

  it('runtime role-write grants exist only with the Glue job permissions boundary, and AttachRolePolicy is not granted (T-03)', () => {
    const boundary = findResource(
      json,
      'AWS::IAM::ManagedPolicy',
      (props) => typeof props.ManagedPolicyName === 'string' && props.ManagedPolicyName.startsWith('AgenticDataIngestion-GlueJobRoleBoundary-'),
    );
    const roleWrites = allStatements(json).filter((s) =>
      actionsOf(s).some((a) => a === 'iam:CreateRole' || a === 'iam:PutRolePolicy'),
    );
    expect(roleWrites.length, 'gluePipelineRole and the job-builder runtime role both create Glue job roles').toBe(2);
    for (const s of roleWrites) {
      expect(
        JSON.stringify((s as any).Condition?.ArnEquals?.['iam:PermissionsBoundary']),
        'CreateRole/PutRolePolicy without the boundary condition lets the caller mint an unbounded role',
      ).toContain(boundary.key);
    }
    const attach = allStatements(json).filter((s) => actionsOf(s).includes('iam:AttachRolePolicy'));
    expect(attach, 'no runtime code attaches a managed policy since T-02').toEqual([]);
  });

  it('the Glue job permissions boundary is never attached to a principal', () => {
    const boundary = findResource(
      json,
      'AWS::IAM::ManagedPolicy',
      (props) => typeof props.ManagedPolicyName === 'string' && props.ManagedPolicyName.startsWith('AgenticDataIngestion-GlueJobRoleBoundary-'),
    );
    expect(boundary.resource.Properties.Roles ?? [], 'the boundary caps; attached, it would grant S3 and secrets on *').toEqual([]);
    for (const resource of Object.values(json.Resources ?? {}) as any[]) {
      if (resource.Type !== 'AWS::IAM::Role') continue;
      expect(JSON.stringify(resource.Properties?.ManagedPolicyArns ?? [])).not.toContain(boundary.key);
    }
  });

  it('every AgentCore runtime role is denied every action on the task-tokens table (T-22)', () => {
    const roles = Object.entries(json.Resources ?? {}).filter(
      ([, r]: [string, any]) =>
        r.Type === 'AWS::IAM::Role' &&
        RUNTIME_ROLE_NAME_PATTERN.test(r.Properties?.RoleName ?? '') &&
        // The retired shared role grants nothing; there is nothing for a Deny to cap.
        !r.Properties.RoleName.endsWith('-agentcore-runtime-role'),
    );
    expect(roles.length, 'nine agent roles and one MCP role').toBe(10);
    for (const [key] of roles) {
      const denies = statementsForRole(json, key).filter((s) => (s as any).Effect === 'Deny');
      const tokenDeny = denies.find((s) =>
        resourcesOf(s).some((r) => JSON.stringify(r).includes('table/agentic_data_ingestion_task_tokens')),
      );
      expect(tokenDeny, `no Deny on agentic_data_ingestion_task_tokens for ${key}`).toBeDefined();
      expect(actionsOf(tokenDeny!)).toContain('dynamodb:*');
      expect(
        resourcesOf(tokenDeny!).some((r) => JSON.stringify(r).includes('table/agentic_data_ingestion_task_tokens/*')),
        'indexes and streams of the table must be denied too',
      ).toBe(true);
    }
  });

  // ── The Lambda-side break-glass grant (a widening past R6.5/R6.7) ──
  //
  // R6.5 and R6.7 name the AgentCore runtime role, and that is the intended
  // route: the live path runs in the container (USE_AGENTCORE_RUNTIME: 'true',
  // compute-stack.ts:432). These assert a DELIBERATE WIDENING past those ACs,
  // so they sit under their own heading rather than inside the R6.5/R6.7 blocks
  // above.
  //
  // The failure they prevent: VERIFICATION_SESSION_ROLE_ARN is published to both
  // environments, so with USE_AGENTCORE_RUNTIME off and VERIFICATION_EXECUTOR=
  // glue, resolve_effective_executor selects `glue` on a populated ARN and the
  // run dies at CreateSession on AccessDenied — a configuration that reads as
  // correct and fails at the last moment.
  //
  // Note what did NOT need amending. The R6.7 assertion above filters every
  // statement in the template for iam:PassRole naming the verification role and
  // requires each to carry the condition, so it already covers this second
  // grantee and passed unchanged. What it does not check is that the grant
  // EXISTS on this role, or its actions and scope — hence the tests below.

  /** Exactly the six APIs glue_session_runner.py calls (verified by reading it). */
  const SESSION_LIFECYCLE_ACTIONS = [
    'glue:CreateSession',
    // CreateSession passes Tags; Glue authorizes them as TagResource on the session.
    'glue:TagResource',
    'glue:GetSession',
    'glue:RunStatement',
    'glue:GetStatement',
    'glue:StopSession',
    'glue:DeleteSession',
  ];
  const CODE_TESTING_ROLE_NAME = `agentic-data-ingestion-${TEST_ENV}-codetesting-role`;

  it('the code-testing role reaches the whole Glue session lifecycle', () => {
    // Against the rendered template, so a grant arriving from a managed policy
    // or a construct helper counts too — the source-text counterpart cannot see
    // those. codeTestingRole carries AgentCorePolicy, which grants glue:Get*
    // (so GetSession/GetStatement are in its ACTION set already) on a resource
    // list with no session ARN — which is precisely why action coverage has to
    // be read together with scope, in the next test.
    const { key } = findResource(
      json,
      'AWS::IAM::Role',
      (props) => props.RoleName === CODE_TESTING_ROLE_NAME,
    );
    const granted = new Set(statementsForRole(json, key).flatMap(actionsOf));
    for (const action of SESSION_LIFECYCLE_ACTIONS) {
      expect(
        granted,
        `${CODE_TESTING_ROLE_NAME} is missing ${action}. The inline verification path ` +
          `(USE_AGENTCORE_RUNTIME off) runs glue_session_runner.py under this role, so ` +
          `an absent action is an AccessDenied mid-run — after a billable session exists`,
      ).toContain(action);
    }
  });

  it('the code-testing session grant is scoped to session/* and carries no glue wildcard', () => {
    // Two properties of one statement, asserted together because either alone
    // is misleading: enumerated actions on a wide resource list, or a tight
    // resource list under glue:*, both defeat the point.
    //
    // The glue:* half also matters across the file: the R6.6 test above requires
    // EVERY glue:* statement in the template to carry both naming forms, so a
    // glue:* here would fail there instead, reading as a naming-fence regression.
    const { key } = findResource(
      json,
      'AWS::IAM::Role',
      (props) => props.RoleName === CODE_TESTING_ROLE_NAME,
    );
    const statements = statementsForRole(json, key);

    const sessionStatements = statements.filter((s) =>
      actionsOf(s).some((a) => SESSION_LIFECYCLE_ACTIONS.includes(a)),
    );
    expect(
      sessionStatements.length,
      'no statement on the code-testing role grants a session-lifecycle action',
    ).toBeGreaterThanOrEqual(1);

    for (const s of sessionStatements) {
      for (const r of resourcesOf(s)) {
        expect(
          r,
          'a session-lifecycle action on the code-testing role reaches outside session/*',
        ).toBe(SESSION_RESOURCE);
      }
      expect(
        actionsOf(s).filter((a) => a === 'glue:*'),
        'glue:* on the code-testing role would hand a Lambda role every Glue action ' +
          'across catalogs, databases, tables, jobs, crawlers and connections to buy ' +
          'six session calls. Enumerate instead (LGR-b46a5bb-2)',
      ).toEqual([]);
    }
  });

  it('the code-testing role\'s iam:PassRole on the verification role is conditioned', () => {
    // This role can now create a Glue session, so an unconditioned PassRole
    // would let it hand any role it can name to a session it controls. Asserted
    // on this role specifically — the R6.7 test above would also catch a missing
    // condition, but it reports it as a fact about the verification role rather
    // than about the principal that gained the capability.
    const { key } = findResource(
      json,
      'AWS::IAM::Role',
      (props) => props.RoleName === CODE_TESTING_ROLE_NAME,
    );
    const passRole = statementsForRole(json, key).filter(
      (s) =>
        actionsOf(s).includes('iam:PassRole') &&
        resourcesOf(s).some((r) => JSON.stringify(r).includes(roleKey)),
    );
    expect(
      passRole.length,
      'no iam:PassRole statement on the code-testing role names the verification role. ' +
        'Without it CreateSession fails on the Role parameter even with every session ' +
        'action granted',
    ).toBeGreaterThanOrEqual(1);

    for (const s of passRole) {
      expect(
        (s as any).Condition?.StringEquals?.['iam:PassedToService'],
        'the PassRole grant on the code-testing role must be conditioned on ' +
          'iam:PassedToService — a principal that can both create a Glue session and ' +
          'pass an unconditioned role can hand any nameable role to a session it controls',
      ).toBe(GLUE_PRINCIPAL);
    }
  });

  it('the widening did not touch the verification session role itself', () => {
    // The change is about the CALLING role. R6.1-R6.3 govern the ASSUMED role and
    // are unchanged: this restates the set equality against the allowlist so that
    // a future edit widening the session role "while adding a caller" fails here
    // with a message saying which of the two roles moved.
    const granted = new Set(statementsForRole(json, roleKey).flatMap(actionsOf));
    expect(
      [...granted].sort(),
      'the verification session role\'s action set changed. It is read-only by design ' +
        '(R6.1-R6.3) and granting a caller the right to pass it is not a reason to ' +
        'widen it — only the calling role should have moved',
    ).toEqual([...VERIFICATION_ROLE_ALLOWED_ACTIONS].sort());
  });
});

// ─────────────────────────────────────────────────────────────────────
// The Glue discovery/runtime crawler role's connection read
//
// Glue validates a crawler's `JdbcTargets[].ConnectionName` by retrieving the
// connection AS THE PASSED ROLE, not as the caller. Both crawler creators run
// under infrastructureRole but pass gluePipelineRole
// (dashboard_api/handlers/jdbc_resources.py `_create_or_update_crawler` for
// wizard-time Detect Schema, and handlers/pipelines_jdbc.py for the runtime
// crawler), so infrastructureRole's own connection/* read does not satisfy the
// validation.
//
// Verified in account 444455556666 / us-west-2 (2026-09-17): three
// CreateCrawler calls identical but for the connection name — the two
// agentic-data-ingestion-* names succeeded, `agentglue-test-fixtures-mysql-source`
// returned `InvalidInputException: Unable to retrieve jdbc connection.`; and
// `iam simulate-principal-policy` for glue:GetConnection on
// agentic-data-ingestion-dev-gluepipeline-role returned implicitDeny for the
// agentglue-test-fixtures-* connections and allowed for
// agentic-data-ingestion-discover-*.
//
// Both assertions below are one fence read from two sides: the read must be
// account-wide (glue_connection mode carries arbitrary customer-chosen names),
// and no connection WRITE may follow it there. The second is the guard against
// a future tidy-up collapsing the new statement into glueAdmin — that is
// glue:*, so adding connection/* to its resource list would hand
// Create/Update/DeleteConnection on every connection in the account to all
// three of glueAdmin's consumers. Pinning an IAM shape with a test rather than
// trusting it follows LGR-fb2310b-1 and LGR-b2f79ef-7.
// ─────────────────────────────────────────────────────────────────────

const GLUE_PIPELINE_ROLE_NAME = `agentic-data-ingestion-${TEST_ENV}-gluepipeline-role`;
/** Connection writes, enumerated: a read-verb prefix test would not catch these. */
const CONNECTION_WRITE_ACTIONS = [
  'glue:CreateConnection',
  'glue:UpdateConnection',
  'glue:DeleteConnection',
];

describe('FoundationStack — the crawler role reads arbitrarily-named Glue connections', () => {
  let json: Record<string, any>;
  let gluePipelineRoleKey: string;

  beforeAll(() => {
    const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
    const stack = new FoundationStack(app, `AgenticDataIngestion-Foundation-${TEST_ENV}-CrawlerConn`, {
      env: { account: TEST_ACCOUNT, region: TEST_REGION },
      projectName: 'agentic-data-ingestion',
      environment: TEST_ENV,
    });
    json = Template.fromStack(stack).toJSON();
    gluePipelineRoleKey = findResource(
      json,
      'AWS::IAM::Role',
      (props) => props.RoleName === GLUE_PIPELINE_ROLE_NAME,
    ).key;
  });

  it('gluePipelineRole can read a Glue connection at any name (connection/*)', () => {
    // Against the rendered template, so the grant counts however it arrives.
    // The account-wide resource is the assertion: glueAdmin already grants
    // glue:GetConnection (via glue:*) on the agentic-data-ingestion-* prefixes,
    // so "the role holds GetConnection somewhere" was already true while
    // Detect Schema was broken.
    const statements = statementsForRole(json, gluePipelineRoleKey);
    expect(statements.length, 'no statements found for the role — assertion vacuous').toBeGreaterThan(0);

    const readsAnyConnection = statements.some(
      (s) =>
        actionsOf(s).includes('glue:GetConnection') &&
        resourcesOf(s).includes(ACCOUNT_WIDE_CONNECTION),
    );
    expect(
      readsAnyConnection,
      `${GLUE_PIPELINE_ROLE_NAME} must hold glue:GetConnection on ${ACCOUNT_WIDE_CONNECTION}. ` +
        `Glue retrieves a crawler's JdbcTargets connection as the PASSED role, so without ` +
        `this every glue_connection-mode name outside agentic-data-ingestion-* fails ` +
        `CreateCrawler with "InvalidInputException: Unable to retrieve jdbc connection."`,
    ).toBe(true);
  });

  it('gluePipelineRole holds no connection WRITE on connection/*', () => {
    // The collapse guard. A statement that grants glue:* (or an enumerated
    // write) on connection/* would satisfy the read test above while handing
    // out account-wide connection create/update/delete — read-wide and
    // write-narrow is the whole shape being pinned. glue:* is checked as well
    // as the three named writes because it contains all of them.
    const offenders = statementsForRole(json, gluePipelineRoleKey)
      .filter((s) => resourcesOf(s).includes(ACCOUNT_WIDE_CONNECTION))
      .flatMap((s) =>
        actionsOf(s).filter((a) => a === 'glue:*' || CONNECTION_WRITE_ACTIONS.includes(a)),
      );
    expect(
      offenders,
      `${GLUE_PIPELINE_ROLE_NAME} grants a connection write on ${ACCOUNT_WIDE_CONNECTION}. The ` +
        `read grant is deliberately separate from glueAdmin: glueAdmin is glue:*, so adding ` +
        `connection/* to its resources leaks Create/Update/DeleteConnection over every ` +
        `connection in the account to all three roles that carry it. Keep the read statement ` +
        `read-only and leave glueAdmin's connection scoping alone`,
    ).toEqual([]);
  });
});

// The schema-mapping Lambda reads sampled source values for its prompt from the
// URI schema_extract stored. On the S3-upload path that URI is the uploaded
// object in the source-files bucket, which the role could not read: every such
// run logged AccessDenied and sent the model no samples. Unit tests stub
// `s3_client`, so only an assertion against the rendered template can see this.
const SCHEMA_MAPPING_ROLE_NAME = `agentic-data-ingestion-${TEST_ENV}-schemamapping-role`;

describe('FoundationStack — the schema mapping role reads the uploaded source object', () => {
  let json: Record<string, any>;
  let schemaMappingRoleKey: string;
  let sourceBucketKey: string;

  beforeAll(() => {
    const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
    const stack = new FoundationStack(app, `AgenticDataIngestion-Foundation-${TEST_ENV}-SchemaMapping`, {
      env: { account: TEST_ACCOUNT, region: TEST_REGION },
      projectName: 'agentic-data-ingestion',
      environment: TEST_ENV,
    });
    json = Template.fromStack(stack).toJSON();
    schemaMappingRoleKey = findResource(
      json,
      'AWS::IAM::Role',
      (props) => props.RoleName === SCHEMA_MAPPING_ROLE_NAME,
    ).key;
    sourceBucketKey = findResource(
      json,
      'AWS::S3::Bucket',
      (props) => props.BucketName === `adi-source-files-${TEST_ACCOUNT}-${TEST_REGION}`,
    ).key;
  });

  /** Statements on the role that name the source-files bucket in any resource. */
  function sourceBucketStatements(): PolicyStatement[] {
    const statements = statementsForRole(json, schemaMappingRoleKey);
    expect(statements.length, 'no statements found for the role — assertion vacuous').toBeGreaterThan(0);
    return statements.filter((s) => resourcesOf(s).some((r) => JSON.stringify(r).includes(sourceBucketKey)));
  }

  it('grants s3:GetObject on the source-files objects', () => {
    const objectReads = sourceBucketStatements().filter(
      (s) =>
        actionsOf(s).includes('s3:GetObject') &&
        resourcesOf(s).some((r) => {
          const text = JSON.stringify(r);
          return text.includes(sourceBucketKey) && text.includes('/*');
        }),
    );
    expect(
      objectReads.length,
      `${SCHEMA_MAPPING_ROLE_NAME} cannot read objects in adi-source-files-*. On the S3-upload ` +
        `path the mapping prompt's sample values come from the uploaded object there, so ` +
        `read_source_samples logs AccessDenied and the model gets no samples`,
    ).toBeGreaterThan(0);
  });

  it('grants only reads on the source-files bucket', () => {
    // The bucket is eventBridgeEnabled and every non-folder key starts a
    // pipeline, so a write from this role would trigger runs.
    const actions = sourceBucketStatements().flatMap(actionsOf);
    expect(actions.length, 'no source-files statement found — assertion vacuous').toBeGreaterThan(0);
    expect(
      actions.filter((a) => a !== 's3:GetObject' && a !== 's3:ListBucket'),
      `${SCHEMA_MAPPING_ROLE_NAME} holds a non-read action on the watched source bucket`,
    ).toEqual([]);
  });
});
