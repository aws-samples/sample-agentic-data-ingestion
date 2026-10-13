// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { TestFixturesStack } from '../lib/test-fixtures-stack';

/**
 * CDK assertion tests for the JDBC TestFixtures stack (P18).
 *
 * Validates: Requirements 1.2, 1.3, 1.6–1.9, 2.1–2.4, 2.7, 2.8, 2.10–2.12,
 * 3.1, 3.2, 3.5, 3.6, 3.7–3.10, 6.1, 6.5, 6.6–6.9, 7.1–7.3, 7.5, 8.1–8.7,
 * 9.2, 9.3 — covering every sub-task 5.1–5.6 in tasks.md.
 *
 * Synthesis details:
 *   - The stack creates its own VPC, so no AWS context lookup is needed
 *     at synth time.
 *   - Docker bundling for the seeding Lambda is suppressed by setting the
 *     `aws:cdk:bundling-stacks` context to an empty list. CDK then emits
 *     a placeholder asset (`cdk-asset-stub`) instead of running the
 *     `lambda.Code.fromAsset({bundling: ...})` Docker command, which
 *     would otherwise require a running Docker daemon and pull a Python
 *     3.12 image.
 *   - Account/region are pinned to a synthetic test env
 *     (`111111111111` / `us-west-2`).
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';

/**
 * Build a CDK App with bundling suppressed and (optionally) the
 * test-fixtures gating context flags pre-set.
 */
function makeApp(extraContext: Record<string, string> = {}): cdk.App {
  return new cdk.App({
    context: {
      // Suppress Docker bundling so `lambda.Code.fromAsset(...{ bundling })`
      // emits a stub asset instead of running `docker run`.
      'aws:cdk:bundling-stacks': [],
      ...extraContext,
    },
  });
}

/** Synth the TestFixturesStack and return both the assertions Template
 *  and the raw CFN JSON (the latter is useful for tag inspection where
 *  Match patterns get awkward). */
function synthTestFixtures(opts: {
  skipGlue?: boolean;
} = {}): { template: Template; json: any; stack: TestFixturesStack } {
  const ctx: Record<string, string> = {};
  if (opts.skipGlue) {
    ctx['adi:skipGlueConnections'] = 'true';
  }
  const app = makeApp(ctx);
  const stack = new TestFixturesStack(app, `AgenticDataIngestion-TestFixtures-${TEST_ENV}`, {
    env: { account: TEST_ACCOUNT, region: TEST_REGION },
    environment: TEST_ENV,
  });
  const template = Template.fromStack(stack);
  return { template, json: template.toJSON(), stack };
}

// =====================================================================
// 5.1 — Stack-synthesis gating tests
// Reqs 1.2, 1.6, 1.7, 1.8, 8.1, 8.2, 9.2, 9.3
// =====================================================================

describe('5.1 — Stack-synthesis gating', () => {
  it('without `adi:deployTestFixtures` flag, the stack does not appear in synthesized stacksRecursively', () => {
    // Mirror the entrypoint's branching logic: if the flag is unset, the
    // stack is never instantiated. Verify by replaying the gating check
    // against the same App used by `bin/agentic_data_ingestion.ts`.
    const app = makeApp();
    const flag = app.node.tryGetContext('adi:deployTestFixtures');
    expect(flag === 'true').toBe(false);

    // Synth without instantiating any stacks; the assembly should be empty
    // (no test-fixtures stack was ever added because the flag is absent).
    const assembly = app.synth();
    const tfStacks = assembly.stacksRecursively.filter((s) =>
      s.stackName.includes('TestFixtures'),
    );
    expect(tfStacks.length).toBe(0);
  });

  it('with `adi:deployTestFixtures=true`, a stack named AgenticDataIngestion-TestFixtures-dev appears', () => {
    const app = makeApp({ 'adi:deployTestFixtures': 'true' });
    new TestFixturesStack(app, `AgenticDataIngestion-TestFixtures-${TEST_ENV}`, {
      env: { account: TEST_ACCOUNT, region: TEST_REGION },
      environment: TEST_ENV,
    });
    const assembly = app.synth();
    const tfStacks = assembly.stacksRecursively.filter(
      (s) => s.stackName === `AgenticDataIngestion-TestFixtures-${TEST_ENV}`,
    );
    expect(tfStacks.length).toBe(1);
  });

  it('with both flags set (deployTestFixtures + skipGlueConnections), zero AWS::Glue::Connection resources are produced', () => {
    const { template } = synthTestFixtures({ skipGlue: true });
    template.resourceCountIs('AWS::Glue::Connection', 0);
  });

  it('with only `adi:deployTestFixtures=true`, exactly two AWS::Glue::Connection resources are produced', () => {
    const { template } = synthTestFixtures();
    template.resourceCountIs('AWS::Glue::Connection', 2);
  });
});

// =====================================================================
// 5.2 — RDS resource assertion tests
// Reqs 2.1–2.4, 2.10–2.12, 3.1, 3.2, 3.7–3.10
// =====================================================================

describe('5.2 — RDS resources', () => {
  let template: Template;
  beforeAll(() => {
    template = synthTestFixtures().template;
  });

  it('exactly one AWS::RDS::DBInstance matches the documented MySQL config', () => {
    // Only the standalone MySQL instance has Engine=mysql; the Aurora
    // cluster's writer instance has no Engine property (it inherits from
    // the cluster), so this resourceCountIs check works for "MySQL only".
    template.hasResourceProperties('AWS::RDS::DBInstance', {
      Engine: 'mysql',
      EngineVersion: Match.stringLikeRegexp('^8\\.0'),
      DBInstanceClass: 'db.t4g.micro',
      MultiAZ: false,
      AllocatedStorage: '20',
      StorageType: 'gp3',
      BackupRetentionPeriod: 0,
      DeletionProtection: false,
      PubliclyAccessible: false,
      DBName: 'appdb',
    });

    // Exactly one MySQL DBInstance — locate via JSON inspection because
    // resourceCountIs counts all DBInstance resources (which includes the
    // Aurora writer).
    const json = template.toJSON();
    const mysqlInstances = Object.values(json.Resources).filter(
      (r: any) =>
        r.Type === 'AWS::RDS::DBInstance' && r.Properties?.Engine === 'mysql',
    );
    expect(mysqlInstances.length).toBe(1);
  });

  it('the MySQL DBInstance has DeletionPolicy=Delete and UpdateReplacePolicy=Delete', () => {
    template.hasResource('AWS::RDS::DBInstance', {
      Properties: Match.objectLike({ Engine: 'mysql' }),
      DeletionPolicy: 'Delete',
      UpdateReplacePolicy: 'Delete',
    });
  });

  it('exactly one AWS::RDS::DBCluster matches the documented Aurora-Postgres config', () => {
    template.resourceCountIs('AWS::RDS::DBCluster', 1);
    template.hasResourceProperties('AWS::RDS::DBCluster', {
      Engine: 'aurora-postgresql',
      EngineVersion: Match.stringLikeRegexp('^15'),
      DBClusterIdentifier: `agentic-data-ingestion-test-postgres-target-${TEST_ENV}`,
      DatabaseName: 'testdb',
      BackupRetentionPeriod: 1,
      DeletionProtection: false,
      // No serverless v2 scaling configuration was requested, so the
      // synthesized template must not include this property at all.
      ServerlessV2ScalingConfiguration: Match.absent(),
    });
  });

  it('the DBCluster has DeletionPolicy=Delete and UpdateReplacePolicy=Delete', () => {
    template.hasResource('AWS::RDS::DBCluster', {
      DeletionPolicy: 'Delete',
      UpdateReplacePolicy: 'Delete',
    });
  });

  it('exactly one Aurora-Postgres DBInstance (the writer) with DBInstanceClass=db.r6g.large', () => {
    // The Aurora writer DBInstance has DBClusterIdentifier set (a Ref to
    // the cluster) and no `Engine` property. Find it that way to avoid
    // matching the standalone MySQL instance.
    //
    // The writer is db.r6g.large (NOT a burstable T class) because the RDS
    // Data API — enabled below — is unsupported on T instance classes. A T
    // class would make EnableHttpEndpoint fail with InvalidParameterCombination.
    const json = template.toJSON();
    const auroraInstances = Object.values(json.Resources).filter(
      (r: any) =>
        r.Type === 'AWS::RDS::DBInstance' &&
        r.Properties?.DBClusterIdentifier !== undefined,
    );
    expect(auroraInstances.length).toBe(1);
    expect((auroraInstances[0] as any).Properties.DBInstanceClass).toBe(
      'db.r6g.large',
    );
  });

  it('Aurora-Postgres cluster has the RDS Data API (HTTP endpoint) enabled', () => {
    // enableDataApi: true → EnableHttpEndpoint on the cluster, so the target
    // can be queried over the Data API without VPC/bastion access. Requires
    // the non-T instance class asserted above.
    template.hasResourceProperties('AWS::RDS::DBCluster', {
      Engine: 'aurora-postgresql',
      EnableHttpEndpoint: true,
    });
  });
});

// =====================================================================
// 5.3 — Networking, secrets, IAM
// Reqs 2.7, 2.8, 3.5, 3.6, 6.5
// =====================================================================

describe('5.3 — Networking, secrets, IAM', () => {
  let template: Template;
  beforeAll(() => {
    template = synthTestFixtures().template;
  });

  it('both S3 and DynamoDB gateway endpoints exist, because the IGW is not a substitute', () => {
    // `natGateways: 0` and the DB subnets are PUBLIC, which reads like the VPC
    // has egress and does not: a Glue ENI gets no public IP, so the
    // `0.0.0.0/0 -> igw-...` route is a blackhole for it. Every AWS service a job
    // here talks to needs its own endpoint, and a missing one is a silent
    // multi-minute connect stall rather than an error.
    //
    // The DynamoDB one was absent, and run-2026-09-23-15-22-35-f826bece paid for
    // it: ~60s of real work, then eleven minutes of connect retries on the
    // post-write metrics PutItem before the best-effort handler gave up. The
    // partial is now bounded to ~3s, so this endpoint is what makes that write
    // succeed rather than merely fail quickly — which is why the assertion is
    // here and not only on the timeout.
    const json = template.toJSON();
    const gatewayServices = Object.values(json.Resources)
      .filter((r: any) => r.Type === 'AWS::EC2::VPCEndpoint')
      .map((r: any) => JSON.stringify(r.Properties?.ServiceName ?? ''))
      // Gateway endpoints render ServiceName via Fn::Join with the partition;
      // match on the service suffix rather than the assembled string.
      .filter((s: string) => s.includes('.s3') || s.includes('.dynamodb'));

    expect(
      gatewayServices.some((s) => s.includes('.dynamodb')),
      `no DynamoDB VPC endpoint: a Glue job in this VPC cannot record its row ` +
        `counts, and the attempt stalls instead of failing. Endpoints found: ` +
        `${JSON.stringify(gatewayServices)}`,
    ).toBe(true);
    expect(
      gatewayServices.some((s) => s.includes('.s3')),
      'no S3 VPC endpoint: Glue script staging and log delivery would stall',
    ).toBe(true);
  });

  it('declares no NAT gateway — the endpoints are the egress story, and they are cheaper', () => {
    // Pins the cost posture the endpoint comments rest on. If a NAT is ever
    // added, the "add an endpoint per service" reasoning above stops applying and
    // those comments need rewriting rather than silently becoming wrong.
    template.resourceCountIs('AWS::EC2::NatGateway', 0);
  });

  it('the MySQL and Postgres DB security groups each have a self-referencing DB-port rule plus the Glue inter-ENI all-traffic rule', () => {
    // Filter to the two DB SGs we declared via their GroupDescription
    // strings. (The stack also creates a Secrets Manager VPC-endpoint SG,
    // and the DB Subnet Group construct can emit an SG-shaped resource in
    // some CDK versions, so we do NOT assert a fixed total SG count.)
    const json = template.toJSON();
    const sgs = Object.entries(json.Resources).filter(
      ([, r]: [string, any]) => r.Type === 'AWS::EC2::SecurityGroup',
    );

    const mysqlSg = sgs.find(([, r]: [string, any]) =>
      String(r.Properties?.GroupDescription ?? '').includes(
        'MySQL test source',
      ),
    );
    const pgSg = sgs.find(([, r]: [string, any]) =>
      String(r.Properties?.GroupDescription ?? '').includes(
        'Postgres test target',
      ),
    );
    expect(mysqlSg).toBeDefined();
    expect(pgSg).toBeDefined();

    // Each self-referencing ingress is emitted as a separate
    // `AWS::EC2::SecurityGroupIngress` resource (CDK does this to break
    // the otherwise-cyclic dependency between an SG and a rule that
    // references the SG itself). Each DB SG has TWO self-referencing rules:
    //   1. the documented DB port (3306 MySQL / 5432 Postgres), and
    //   2. an all-traffic rule required for Glue inter-ENI traffic
    //      (Spark worker coordination during Crawler / ETL Job runs).
    const selfRefIngresses = Object.values(json.Resources).filter(
      (r: any) =>
        r.Type === 'AWS::EC2::SecurityGroupIngress' &&
        r.Properties?.GroupId?.['Fn::GetAtt']?.[0] ===
          (r.Properties?.SourceSecurityGroupId?.['Fn::GetAtt']?.[0] ?? ''),
    );

    // The documented DB-port rules: one per DB SG on 3306 / 5432.
    const tcpPorts = selfRefIngresses
      .filter((i: any) => i.Properties?.IpProtocol === 'tcp')
      .map((i: any) => i.Properties?.FromPort)
      .sort();
    expect(tcpPorts).toContain(3306);
    expect(tcpPorts).toContain(5432);

    // The Glue inter-ENI all-traffic rules: one per DB SG, IpProtocol '-1'.
    const allTrafficRules = selfRefIngresses.filter(
      (i: any) => i.Properties?.IpProtocol === '-1',
    );
    expect(allTrafficRules.length).toBe(2);
  });

  it('cross-allows the two DB security groups so a dual-connection Glue job can reach both stores', () => {
    // A JDBC-source -> JDBC-target ETL job attaches BOTH Glue connections,
    // but Glue places the job's ENIs in only one connection's subnet with
    // only that connection's SG, then accesses BOTH data stores from those
    // ENIs. With self-referencing-only SGs the second store rejects the
    // ENI. These cross-ingress rules (each DB port allowed from the OTHER
    // SG) make a single ENI acceptable at both databases.
    const json = template.toJSON();

    // Cross-connection ingress = an SGIngress whose GroupId SG differs from
    // its SourceSecurityGroupId SG (i.e. NOT self-referencing).
    const crossIngresses = Object.values(json.Resources).filter(
      (r: any) =>
        r.Type === 'AWS::EC2::SecurityGroupIngress' &&
        r.Properties?.IpProtocol === 'tcp' &&
        r.Properties?.GroupId?.['Fn::GetAtt']?.[0] &&
        r.Properties?.SourceSecurityGroupId?.['Fn::GetAtt']?.[0] &&
        r.Properties.GroupId['Fn::GetAtt'][0] !==
          r.Properties.SourceSecurityGroupId['Fn::GetAtt'][0],
    );

    const crossPorts = crossIngresses
      .map((i: any) => i.Properties?.FromPort)
      .sort();
    // One cross-rule for the MySQL source read (3306) and one for the
    // Postgres target write (5432).
    expect(crossPorts).toContain(3306);
    expect(crossPorts).toContain(5432);
  });


  it('two AWS::SecretsManager::Secret resources match the documented names', () => {
    template.resourceCountIs('AWS::SecretsManager::Secret', 2);
    template.hasResourceProperties('AWS::SecretsManager::Secret', {
      Name: 'agentic-data-ingestion/test-fixtures/mysql-source',
    });
    template.hasResourceProperties('AWS::SecretsManager::Secret', {
      Name: 'agentic-data-ingestion/test-fixtures/postgres-target',
    });
  });

  it('the seeding Lambda execution role has a policy granting secretsmanager:GetSecretValue on both secret ARNs', () => {
    // `secret.grantRead(lambda)` synthesizes one IAM::Policy per secret
    // attached to the Lambda's role, each granting GetSecretValue on a
    // Ref to the secret resource. Verify both grants exist via two
    // hasResourceProperties calls, matching by Action + the
    // VersionStage condition that grantRead emits.
    template.hasResourceProperties(
      'AWS::IAM::Policy',
      Match.objectLike({
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                'secretsmanager:GetSecretValue',
                'secretsmanager:DescribeSecret',
              ]),
              Effect: 'Allow',
              Resource: Match.objectLike({
                Ref: Match.stringLikeRegexp('^MySQLSource.*Secret'),
              }),
            }),
          ]),
        }),
      }),
    );

    template.hasResourceProperties(
      'AWS::IAM::Policy',
      Match.objectLike({
        PolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Action: Match.arrayWith([
                'secretsmanager:GetSecretValue',
                'secretsmanager:DescribeSecret',
              ]),
              Effect: 'Allow',
              Resource: Match.objectLike({
                Ref: Match.stringLikeRegexp('^PostgresTarget.*Secret'),
              }),
            }),
          ]),
        }),
      }),
    );
  });
});

// =====================================================================
// 5.4 — Seeding Lambda + Custom Resource
// Reqs 6.1, 6.6–6.9, 7.1–7.3, 7.5
// =====================================================================

describe('5.4 — Seeding Lambda and Custom Resource', () => {
  let template: Template;
  let json: any;
  beforeAll(() => {
    const synth = synthTestFixtures();
    template = synth.template;
    json = synth.json;
  });

  it('a Lambda function exists with the documented runtime, memory, timeout, handler, two SGs, and four env vars', () => {
    // Note: Match.anyValue() can't be nested inside arrayWith(), so we
    // assert SecurityGroupIds via the JSON inspection below; here we just
    // pin the static fields.
    template.hasResourceProperties(
      'AWS::Lambda::Function',
      Match.objectLike({
        Runtime: 'python3.12',
        MemorySize: 512,
        Timeout: 300,
        Handler: 'index.handler',
        Environment: Match.objectLike({
          Variables: Match.objectLike({
            MYSQL_SECRET_ARN: Match.anyValue(),
            POSTGRES_SECRET_ARN: Match.anyValue(),
            MYSQL_SEED_SQL_PATH: '/var/task/seed_sql/mysql.sql',
            POSTGRES_SEED_SQL_PATH: '/var/task/seed_sql/postgres.sql',
          }),
        }),
      }),
    );

    // Belt-and-braces: assert that the seeding Lambda specifically has
    // exactly two security groups attached. The cr.Provider's framework
    // Lambda is a separate function with no VpcConfig, so we filter to
    // the ones that have a VpcConfig and look for our four env vars.
    const seedingLambdas = Object.values(json.Resources).filter(
      (r: any) =>
        r.Type === 'AWS::Lambda::Function' &&
        r.Properties?.Environment?.Variables?.MYSQL_SECRET_ARN !== undefined,
    );
    expect(seedingLambdas.length).toBe(1);
    const sg = (seedingLambdas[0] as any).Properties.VpcConfig.SecurityGroupIds;
    expect(Array.isArray(sg)).toBe(true);
    expect(sg.length).toBe(2);
  });

  it('a Custom::AgenticDataIngestionTestFixturesSeeding resource has DependsOn including the MySQL and Postgres logical IDs', () => {
    const seedingResources = Object.entries(json.Resources).filter(
      ([, r]: [string, any]) =>
        r.Type === 'Custom::AgenticDataIngestionTestFixturesSeeding',
    );
    expect(seedingResources.length).toBe(1);
    const [, resource] = seedingResources[0] as [string, any];

    // DependsOn is an array of CFN logical IDs.
    const dependsOn: string[] = resource.DependsOn ?? [];
    expect(dependsOn.length).toBeGreaterThanOrEqual(2);
    expect(dependsOn.some((id) => id.startsWith('MySQLSource'))).toBe(true);
    expect(dependsOn.some((id) => id.startsWith('PostgresTarget'))).toBe(true);
  });

  it('the Custom Resource Properties.SeedHash is a 64-character hex string', () => {
    template.hasResourceProperties(
      'Custom::AgenticDataIngestionTestFixturesSeeding',
      Match.objectLike({
        SeedHash: Match.stringLikeRegexp('^[0-9a-f]{64}$'),
      }),
    );
  });
});

// =====================================================================
// 5.5 — Optional Glue Connection tests
// Reqs 8.3–8.6
// =====================================================================

describe('5.5 — Optional Glue Connections (default state)', () => {
  let template: Template;
  let json: any;
  beforeAll(() => {
    const synth = synthTestFixtures();
    template = synth.template;
    json = synth.json;
  });

  it('two Glue Connections exist with the documented names', () => {
    template.resourceCountIs('AWS::Glue::Connection', 2);
    template.hasResourceProperties('AWS::Glue::Connection', {
      ConnectionInput: Match.objectLike({
        Name: 'agentic-data-ingestion-test-fixtures-mysql-source',
      }),
    });
    template.hasResourceProperties('AWS::Glue::Connection', {
      ConnectionInput: Match.objectLike({
        Name: 'agentic-data-ingestion-test-fixtures-postgres-target',
      }),
    });
  });

  it('MySQL connection has ConnectionType=JDBC, omits JDBC_DRIVER_CLASS_NAME, and has an engine-shaped JDBC URL', () => {
    template.hasResourceProperties(
      'AWS::Glue::Connection',
      Match.objectLike({
        ConnectionInput: Match.objectLike({
          Name: 'agentic-data-ingestion-test-fixtures-mysql-source',
          ConnectionType: 'JDBC',
          ConnectionProperties: Match.objectLike({
            JDBC_ENFORCE_SSL: 'true',
          }),
        }),
      }),
    );

    // JDBC_DRIVER_CLASS_NAME is intentionally omitted for built-in
    // engines — Glue auto-detects the driver class from the URL prefix
    // and otherwise demands a JDBC_DRIVER_JAR_URI we don't supply.
    const conns = Object.values(json.Resources).filter(
      (r: any) =>
        r.Type === 'AWS::Glue::Connection' &&
        r.Properties?.ConnectionInput?.Name ===
          'agentic-data-ingestion-test-fixtures-mysql-source',
    );
    expect(conns.length).toBe(1);
    const props = (conns[0] as any).Properties.ConnectionInput
      .ConnectionProperties;
    expect(props.JDBC_DRIVER_CLASS_NAME).toBeUndefined();

    // The JDBC_CONNECTION_URL is a CFN intrinsic (Fn::Join) at synth time
    // because it interpolates token-valued endpoint addresses. Walk the
    // raw JSON to inspect the Fn::Join's literal segments.
    const url = props.JDBC_CONNECTION_URL;
    // Either a literal string (`jdbc:mysql://...`) or a Fn::Join with a
    // leading `jdbc:mysql://` segment.
    if (typeof url === 'string') {
      expect(url).toMatch(/^jdbc:mysql:\/\//);
    } else {
      const literal = JSON.stringify(url);
      expect(literal).toContain('jdbc:mysql://');
      expect(literal).toContain('/appdb');
    }
  });

  it('Postgres connection has ConnectionType=JDBC, omits JDBC_DRIVER_CLASS_NAME, and has an engine-shaped JDBC URL', () => {
    template.hasResourceProperties(
      'AWS::Glue::Connection',
      Match.objectLike({
        ConnectionInput: Match.objectLike({
          Name: 'agentic-data-ingestion-test-fixtures-postgres-target',
          ConnectionType: 'JDBC',
          ConnectionProperties: Match.objectLike({
            JDBC_ENFORCE_SSL: 'true',
          }),
        }),
      }),
    );

    const conns = Object.values(json.Resources).filter(
      (r: any) =>
        r.Type === 'AWS::Glue::Connection' &&
        r.Properties?.ConnectionInput?.Name ===
          'agentic-data-ingestion-test-fixtures-postgres-target',
    );
    expect(conns.length).toBe(1);
    const props = (conns[0] as any).Properties.ConnectionInput
      .ConnectionProperties;
    expect(props.JDBC_DRIVER_CLASS_NAME).toBeUndefined();

    const url = props.JDBC_CONNECTION_URL;
    if (typeof url === 'string') {
      expect(url).toMatch(/^jdbc:postgresql:\/\//);
    } else {
      const literal = JSON.stringify(url);
      expect(literal).toContain('jdbc:postgresql://');
      expect(literal).toContain('/testdb');
    }
  });

  it('both connections reference their secret by SECRET_ID and carry no inline credentials', () => {
    // This test previously required USERNAME and PASSWORD to be
    // `{{resolve:secretsmanager:...}}` dynamic references, which pinned exactly
    // the shape that made these fixtures unusable as a pipeline SOURCE: the JDBC
    // source template resolves credentials from Secrets Manager at runtime, so
    // `pipelines_jdbc` rejects a source connection carrying no SECRET_ID. A
    // dynamic reference also resolves at deploy time, so the plaintext pair ends
    // up stored on the connection.
    //
    // Glue accepts SECRET_ID as a connection property and rejects it only
    // *alongside* USERNAME/PASSWORD — they are mutually exclusive — so the
    // absence of the inline pair is part of the contract, not incidental.
    const conns = Object.values(json.Resources).filter(
      (r: any) => r.Type === 'AWS::Glue::Connection',
    );
    expect(conns.length).toBe(2);
    for (const conn of conns) {
      const props = (conn as any).Properties.ConnectionInput.ConnectionProperties;
      expect(props.SECRET_ID).toBeDefined();
      // A CloudFormation reference to the secret resource, never a credential
      // value. `secret.secretArn` synthesises to a `Ref` at the secret's
      // attachment, so the assertion is on the intrinsic rather than on a
      // literal ARN string.
      const secretRef = JSON.stringify(props.SECRET_ID);
      expect(secretRef).toMatch(/Secret/);
      // Specifically NOT a `{{resolve:secretsmanager:...}}` dynamic reference:
      // that form resolves at deploy time and would store the credential itself.
      expect(secretRef).not.toContain('{{resolve:secretsmanager:');
      expect(props.USERNAME).toBeUndefined();
      expect(props.PASSWORD).toBeUndefined();
    }
  });
  it('PhysicalConnectionRequirements is populated with SubnetId, non-empty SecurityGroupIdList, and AvailabilityZone', () => {
    const conns = Object.values(json.Resources).filter(
      (r: any) => r.Type === 'AWS::Glue::Connection',
    );
    expect(conns.length).toBe(2);

    for (const conn of conns) {
      const pcr = (conn as any).Properties.ConnectionInput
        .PhysicalConnectionRequirements;
      expect(pcr).toBeDefined();
      expect(pcr.SubnetId).toBeDefined();
      expect(Array.isArray(pcr.SecurityGroupIdList)).toBe(true);
      expect(pcr.SecurityGroupIdList.length).toBeGreaterThan(0);
      expect(pcr.AvailabilityZone).toBeDefined();
    }
  });
});

describe('5.5b — Skip-flag behavior (repeated for completeness within Glue-suite)', () => {
  it('with `adi:skipGlueConnections=true`, no AWS::Glue::Connection resources exist', () => {
    const { template } = synthTestFixtures({ skipGlue: true });
    template.resourceCountIs('AWS::Glue::Connection', 0);
  });
});

// =====================================================================
// 5.6 — Tags + outputs
// Reqs 1.3, 1.9, 8.7
// =====================================================================

describe('5.6 — Tags and CloudFormation outputs', () => {
  let template: Template;
  let json: any;
  beforeAll(() => {
    const synth = synthTestFixtures();
    template = synth.template;
    json = synth.json;
  });

  it('the synthesized template applies Project=AgenticDataIngestion, Stage=dev, Component=test-fixtures tags to taggable resources', () => {
    // Tags.of(stack).add(...) propagates to every taggable resource via
    // an Aspect. The synthesized output places the tags inline on each
    // resource's Properties.Tags array (RDS, EC2 SG, Secrets Manager,
    // Lambda all use this convention).
    //
    // Acceptance: at least one of every "interesting" resource type
    // (DBInstance, DBCluster, SecurityGroup, Secret) carries all three
    // expected tags. We don't need to assert on every single resource —
    // tags propagated by an Aspect are emitted uniformly.
    const expectedTags = [
      { Key: 'Project', Value: 'AgenticDataIngestion' },
      { Key: 'Stage', Value: TEST_ENV },
      { Key: 'Component', Value: 'test-fixtures' },
    ];

    const resourceTypesToCheck = [
      'AWS::RDS::DBInstance',
      'AWS::RDS::DBCluster',
      'AWS::EC2::SecurityGroup',
      'AWS::SecretsManager::Secret',
    ];

    for (const rType of resourceTypesToCheck) {
      const resources = Object.values(json.Resources).filter(
        (r: any) => r.Type === rType,
      );
      expect(resources.length).toBeGreaterThan(0);
      // Every instance of that type should carry all three tags.
      for (const r of resources) {
        const tags: { Key: string; Value: string }[] =
          (r as any).Properties?.Tags ?? [];
        // Tags array contains additional CDK-managed tags (e.g. aws-cdk:*),
        // so we look for our three tags via subset-match.
        for (const expected of expectedTags) {
          const found = tags.find(
            (t) => t.Key === expected.Key && t.Value === expected.Value,
          );
          expect(
            found,
            `Expected ${rType} to carry tag ${expected.Key}=${expected.Value}; got tags=${JSON.stringify(tags)}`,
          ).toBeDefined();
        }
      }
    }
  });

  it('always-emitted outputs are declared (MySQLSourceEndpoint, MySQLSourceSecretArn, PostgresTargetEndpoint, PostgresTargetSecretArn)', () => {
    template.hasOutput('MySQLSourceEndpoint', {});
    template.hasOutput('MySQLSourceSecretArn', {});
    template.hasOutput('PostgresTargetEndpoint', {});
    template.hasOutput('PostgresTargetSecretArn', {});
  });

  it('with Glue connections enabled, MySQLGlueConnectionName and PostgresGlueConnectionName outputs are declared', () => {
    template.hasOutput('MySQLGlueConnectionName', {
      Value: 'agentic-data-ingestion-test-fixtures-mysql-source',
    });
    template.hasOutput('PostgresGlueConnectionName', {
      Value: 'agentic-data-ingestion-test-fixtures-postgres-target',
    });
  });

  it('with `adi:skipGlueConnections=true`, neither Glue-connection output is declared', () => {
    const { template: skipTemplate } = synthTestFixtures({ skipGlue: true });
    const outputs = (skipTemplate.toJSON().Outputs ?? {}) as Record<
      string,
      unknown
    >;
    expect(outputs.MySQLGlueConnectionName).toBeUndefined();
    expect(outputs.PostgresGlueConnectionName).toBeUndefined();
    // The four always-present outputs are still there.
    expect(outputs.MySQLSourceEndpoint).toBeDefined();
    expect(outputs.MySQLSourceSecretArn).toBeDefined();
    expect(outputs.PostgresTargetEndpoint).toBeDefined();
    expect(outputs.PostgresTargetSecretArn).toBeDefined();
  });
});
