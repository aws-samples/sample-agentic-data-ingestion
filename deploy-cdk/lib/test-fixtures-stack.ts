// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as path from 'path';
import * as fs from 'fs';
import * as crypto from 'crypto';
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
// `iam` is imported per the design's import list even though grants flow through
// `secret.grantRead` so the principal-policy edits happen via aws-cdk-lib's
// internal IAM constructs without us referencing them by name.
// eslint-disable-next-line @typescript-eslint/no-unused-vars
import * as iam from 'aws-cdk-lib/aws-iam';
import * as glue from 'aws-cdk-lib/aws-glue';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';
import { pinnedBuildImage, SAM_PYTHON312_BUILD_IMAGE } from './build-images';

/**
 * Props for {@link TestFixturesStack}.
 *
 * `environment` is the deployment environment label (e.g., "dev"). It is appended
 * to physical resource identifiers such as the MySQL instance identifier and the
 * Aurora Postgres cluster identifier so the stack does not collide with future
 * staging/prod variants.
 */
export interface TestFixturesStackProps extends cdk.StackProps {
  environment: string;
}

/**
 * Opt-in seventh CDK stack that provisions JDBC test databases for AgenticDataIngestion.
 *
 * Provisions:
 *   - MySQL 8.0 source instance (`agentic-data-ingestion-test-mysql-source-<env>`) seeded with
 *     `customers` (50 rows) and `orders` (150 rows) via a Lambda-backed CFN
 *     Custom Resource.
 *   - Aurora Postgres 15 cluster (`agentic-data-ingestion-test-postgres-target-<env>`) with a
 *     single writer and an empty `testdb` database.
 *   - Two Secrets Manager secrets, two self-referencing security groups, the
 *     seeding Lambda, the cr.Provider/CustomResource pair, and (optionally) two
 *     pre-created Glue Connections for the wizard's `glue_connection` mode.
 *
 * The stack is gated at the entrypoint by the CDK context flag
 * `adi:deployTestFixtures=true` (see `bin/agentic_data_ingestion.ts`); a default
 * `cdk deploy --all` does NOT instantiate this stack. The optional Glue
 * Connections can be skipped within an already-deploying stack via
 * `adi:skipGlueConnections=true`.
 *
 * VPC: creates a small dedicated VPC (no NAT gateway) with an S3 gateway
 * endpoint for the fixture databases, so the stack deploys into any account
 * without pre-existing network dependencies.
 */
export class TestFixturesStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: TestFixturesStackProps) {
    super(scope, id, props);

    // 1. Tag every taggable resource the stack creates (Req 1.3)
    cdk.Tags.of(this).add('Project', 'AgenticDataIngestion');
    cdk.Tags.of(this).add('Stage', props.environment);
    cdk.Tags.of(this).add('Component', 'test-fixtures');

    // 2. Create a dedicated VPC for JDBC testing.
    //
    // This VPC includes:
    //   - 2 public subnets (for RDS instances, seeding Lambda, Glue ENIs)
    //   - 2 private isolated subnets (for enhanced isolation if needed)
    //   - Internet Gateway for outbound connectivity
    //   - S3 VPC Gateway Endpoint (required for Glue)
    //   - Secrets Manager VPC Interface Endpoint (for Lambda credential access)
    //
    // RDS instances are placed in public subnets with `publiclyAccessible: false`
    // and self-referencing security groups, so they're NOT reachable from the
    // internet but ARE reachable from Glue ENIs that join the same SG.
    const vpc = new ec2.Vpc(this, 'TestFixturesVpc', {
      vpcName: `agentic-data-ingestion-test-fixtures-vpc-${props.environment}`,
      ipAddresses: ec2.IpAddresses.cidr('10.200.0.0/16'),
      maxAzs: 2,
      // No NAT gateway, deliberately — it is the most expensive thing this stack
      // could contain and the fixtures do not need general egress.
      //
      // The IGW is NOT a substitute for AWS-service reachability, and the comment
      // here used to imply it was ("using public subnets + IGW"). A Glue ENI gets
      // no public IP, so a `0.0.0.0/0 -> igw-...` route is a blackhole for it: the
      // route exists, the packets go nowhere, and the caller sees a connect
      // timeout rather than a refusal. Every AWS service a job in this VPC talks
      // to therefore needs its own VPC endpoint — see the S3 and DynamoDB gateway
      // endpoints and the Secrets Manager interface endpoint below. Adding a
      // service without adding its endpoint produces a silent multi-minute stall,
      // not an error.
      natGateways: 0,
      subnetConfiguration: [
        {
          name: 'Public',
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
        {
          name: 'Isolated',
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
          cidrMask: 24,
        },
      ],
    });

    const dbSubnets = vpc.selectSubnets({
      subnetType: ec2.SubnetType.PUBLIC,
    });

    // S3 VPC Gateway Endpoint — required for Glue Crawlers and Glue ETL
    // jobs running inside the VPC to reach S3 for script staging and
    // log delivery. Gateway endpoints are free (no hourly charge).
    vpc.addGatewayEndpoint('S3GatewayEndpoint', {
      service: ec2.GatewayVpcEndpointAwsService.S3,
    });

    // DynamoDB VPC Gateway Endpoint — required for the same reason as the S3 one
    // above, and for a concrete failure. The generated Glue job records its
    // Rows_Read / Rows_Written to the state table after the target write
    // (`pyspark_templates/_partials/_load_counts.py.j2`). With no route to
    // DynamoDB that PutItem cannot complete, and on
    // run-2026-09-23-15-22-35-f826bece it did not fail fast either: the job did
    // ~60s of real work and then sat for ELEVEN MINUTES on connect retries
    // before its best-effort handler gave up, ending with "Connect timeout on
    // endpoint URL: https://dynamodb.us-west-2.amazonaws.com/" and no metrics
    // row. The partial is now bounded to ~3s, so this endpoint is what makes the
    // write actually SUCCEED rather than merely fail quickly.
    //
    // A gateway endpoint, not an interface one: DynamoDB and S3 are the only two
    // services that offer the gateway type, it carries no hourly charge, and it
    // needs no security group or subnet placement.
    //
    // This does NOT make the VPC generally egressed, deliberately — `natGateways:
    // 0` above stays, and anything else the job needs has to be added as its own
    // endpoint, which is the least-privilege shape and the cheap one.
    vpc.addGatewayEndpoint('DynamoDbGatewayEndpoint', {
      service: ec2.GatewayVpcEndpointAwsService.DYNAMODB,
    });

    // 3. Security groups — self-referencing only (Req 2.7, 3.5)
    const mysqlSg = new ec2.SecurityGroup(this, 'MySQLSourceSecurityGroup', {
      vpc,
      description: 'Self-referencing ingress for MySQL test source',
      allowAllOutbound: true,
    });
    mysqlSg.addIngressRule(
      mysqlSg,
      ec2.Port.tcp(3306),
      'Self-referencing ingress for MySQL source',
    );
    // Glue requires the connection's security group to allow ALL ports
    // from a self-referencing source. This is for inter-ENI traffic
    // during Glue job execution (Spark workers talking to each other,
    // Glue's internal coordination protocol). Without this, Glue
    // CreateCrawler / StartJobRun fails with "At least one security
    // group must open all ingress ports".
    mysqlSg.addIngressRule(
      mysqlSg,
      ec2.Port.allTraffic(),
      'Glue inter-ENI traffic (required for Glue Crawler / ETL Job)',
    );

    const pgSg = new ec2.SecurityGroup(this, 'PostgresTargetSecurityGroup', {
      vpc,
      description: 'Self-referencing ingress for Postgres test target',
      allowAllOutbound: true,
    });
    pgSg.addIngressRule(
      pgSg,
      ec2.Port.tcp(5432),
      'Self-referencing ingress for Postgres target',
    );
    // Same Glue inter-ENI requirement as MySQL_Source_SecurityGroup.
    pgSg.addIngressRule(
      pgSg,
      ec2.Port.allTraffic(),
      'Glue inter-ENI traffic (required for Glue Crawler / ETL Job)',
    );

    // Cross-connection ingress for JDBC-source -> JDBC-target ETL jobs.
    //
    // A Glue ETL job that reads from MySQL (source connection) and writes to
    // Postgres (target connection) attaches BOTH Glue connections. Glue
    // creates the job's ENIs in ONLY ONE connection's subnet and associates
    // ONLY THAT connection's security group with them, then accesses BOTH
    // data stores from that single set of ENIs (confirmed AWS Glue behaviour
    // for multi-connection jobs). Because both SGs above are self-referencing
    // only, an ENI carrying just one SG is rejected by the other database —
    // the job's source read OR target write fails with "connection attempt
    // failed" depending on which connection Glue chose for placement.
    //
    // Allowing each database's port from the OTHER connection's SG makes a
    // single ENI (carrying either SG) acceptable at both databases, so the
    // dual-connection job can reach MySQL:3306 and Postgres:5432 regardless
    // of which connection Glue uses for ENI placement.
    mysqlSg.addIngressRule(
      pgSg,
      ec2.Port.tcp(3306),
      'Glue dual-connection ETL: allow MySQL source read from target-SG ENIs',
    );
    pgSg.addIngressRule(
      mysqlSg,
      ec2.Port.tcp(5432),
      'Glue dual-connection ETL: allow Postgres target write from source-SG ENIs',
    );

    // 4. MySQL_Source — db.t4g.micro, single-AZ, 20 GB gp3 (Req 2.*)
    const mysqlSource = new rds.DatabaseInstance(this, 'MySQLSource', {
      instanceIdentifier: `agentic-data-ingestion-test-mysql-source-${props.environment}`,
      engine: rds.DatabaseInstanceEngine.mysql({
        version: rds.MysqlEngineVersion.VER_8_0,
      }),
      instanceType: ec2.InstanceType.of(
        ec2.InstanceClass.T4G,
        ec2.InstanceSize.MICRO,
      ),
      vpc,
      vpcSubnets: dbSubnets,
      securityGroups: [mysqlSg],
      multiAz: false,
      allocatedStorage: 20,
      storageType: rds.StorageType.GP3,
      databaseName: 'appdb',
      credentials: rds.Credentials.fromGeneratedSecret('agentic_data_ingestion_admin', {
        secretName: 'agentic-data-ingestion/test-fixtures/mysql-source',
      }),
      backupRetention: cdk.Duration.days(0),
      deletionProtection: false,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      publiclyAccessible: false,
    });

    // 5. Postgres_Target — Aurora Postgres 15, single writer db.r6g.large (Req 3.*)
    // Instance class is db.r6g.large (NOT a burstable T class): the RDS Data
    // API is unsupported on T instance classes, and we enable it below via
    // `enableDataApi` so the cluster can be queried over the HTTP endpoint
    // (e.g. SELECT count(*) checks) without VPC/bastion access. A smaller T
    // class would make `enableDataApi` fail at deploy with
    // InvalidParameterCombination ("instance class ... doesn't support the
    // HTTP endpoint for using RDS Data API").
    const pgTarget = new rds.DatabaseCluster(this, 'PostgresTarget', {
      clusterIdentifier: `agentic-data-ingestion-test-postgres-target-${props.environment}`,
      engine: rds.DatabaseClusterEngine.auroraPostgres({
        version: rds.AuroraPostgresEngineVersion.VER_15_15,
      }),
      // Data API requires a non-T (memory-optimized) instance class.
      enableDataApi: true,
      writer: rds.ClusterInstance.provisioned('Writer', {
        instanceType: ec2.InstanceType.of(
          ec2.InstanceClass.R6G,
          ec2.InstanceSize.LARGE,
        ),
      }),
      vpc,
      vpcSubnets: dbSubnets,
      securityGroups: [pgSg],
      defaultDatabaseName: 'testdb',
      credentials: rds.Credentials.fromGeneratedSecret('agentic_data_ingestion_admin', {
        secretName: 'agentic-data-ingestion/test-fixtures/postgres-target',
      }),
      // Aurora minimum retention is 1 day; 0 is rejected at deploy time.
      backup: { retention: cdk.Duration.days(1) },
      deletionProtection: false,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // 6. Seeding_Lambda + content-hash trigger (Req 6.*, Req 7.3)
    const seedSqlDir = path.join(
      __dirname,
      '..',
      '..',
      'tests',
      'fixtures',
      'seed_sql',
    );
    const lambdaCodeDir = path.join(
      __dirname,
      '..',
      '..',
      'tests',
      'fixtures',
      'cdk',
      'seeding_lambda',
    );

    // SHA-256 over the concatenation of mysql.sql + postgres.sql bytes. Used as
    // a CustomResource property so an Update event fires only when seed SQL
    // content actually changes between deploys.
    const seedHash = crypto
      .createHash('sha256')
      .update(fs.readFileSync(path.join(seedSqlDir, 'mysql.sql')))
      .update(fs.readFileSync(path.join(seedSqlDir, 'postgres.sql')))
      .digest('hex');

    // The seed_sql directory lives outside lambdaCodeDir (Docker bundling only
    // mounts lambdaCodeDir at /asset-input by default), so we mount it as an
    // additional read-only volume at /seed_sql_input. This lets the bundling
    // command `cp /seed_sql_input/*.sql /asset-output/seed_sql/` succeed.
    const seedingLambda = new lambda.Function(this, 'SeedingLambda', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(lambdaCodeDir, {
        // Fingerprint-only (bundling bind-mounts the unfiltered source), but
        // the bundling command does `cp -r . /asset-output/`, so anything left
        // in this directory also ships to Lambda. __pycache__ here would both
        // pad the package and move the asset hash after any local test run.
        ignoreMode: cdk.IgnoreMode.GIT,
        exclude: ['__pycache__', '*.pyc', '*.pyo', '.pytest_cache', '.DS_Store'],
        bundling: {
          // Pinned by digest (R-13). Runtime.PYTHON_3_12.bundlingImage resolves to
          // the untagged, i.e. :latest, SAM build image; this is that image's index.
          image: pinnedBuildImage(SAM_PYTHON312_BUILD_IMAGE),
          volumes: [
            {
              hostPath: seedSqlDir,
              containerPath: '/seed_sql_input',
            },
          ],
          command: [
            'bash',
            '-c',
            [
              'pip install -r requirements.txt -t /asset-output',
              'cp -r . /asset-output/',
              'mkdir -p /asset-output/seed_sql',
              'cp /seed_sql_input/*.sql /asset-output/seed_sql/',
            ].join(' && '),
          ],
        },
      }),
      vpc,
      vpcSubnets: dbSubnets,
      // The dev VPC has no PRIVATE_WITH_EGRESS subnets (no NAT gateway), so
      // we place the seeding Lambda in PUBLIC subnets and rely on the
      // self-referencing security group + locked-down outbound rules for
      // network safety. The Lambda needs IGW egress to reach Secrets Manager.
      // Network reachability to the DBs themselves is established by joining
      // both DB security groups, not by being in the same subnet.
      allowPublicSubnet: true,
      securityGroups: [mysqlSg, pgSg],
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      logRetention: logs.RetentionDays.ONE_WEEK,
      environment: {
        MYSQL_SECRET_ARN: mysqlSource.secret!.secretArn,
        POSTGRES_SECRET_ARN: pgTarget.secret!.secretArn,
        MYSQL_SEED_SQL_PATH: '/var/task/seed_sql/mysql.sql',
        POSTGRES_SEED_SQL_PATH: '/var/task/seed_sql/postgres.sql',
      },
    });

    // Grant secretsmanager:GetSecretValue scoped to the two test-fixture secrets.
    mysqlSource.secret!.grantRead(seedingLambda);
    pgTarget.secret!.grantRead(seedingLambda);

    // 6b. Secrets Manager VPC interface endpoint (PrivateLink).
    //
    // The seeding Lambda is in a PUBLIC subnet for DB reachability (the dev
    // VPC has no NAT gateway and therefore no PRIVATE_WITH_EGRESS subnets),
    // but Lambda ENIs in public subnets do not auto-assign public IPs, so
    // they cannot egress to Secrets Manager's public endpoint via the IGW.
    // Adding a VPC interface endpoint for Secrets Manager routes the
    // Lambda's `GetSecretValue` calls over PrivateLink instead — staying
    // entirely inside the VPC.
    //
    // Cost: ≈ $7/month/AZ for the endpoint plus per-GB data processed.
    // The endpoint sits in the same subnets and SG as the seeding Lambda
    // so the Lambda can reach it without crossing the IGW.
    const secretsEndpointSg = new ec2.SecurityGroup(this, 'SecretsEndpointSg', {
      vpc,
      description: 'Allow seeding Lambda to reach Secrets Manager VPC endpoint',
      allowAllOutbound: true,
    });
    secretsEndpointSg.addIngressRule(
      ec2.Peer.securityGroupId(mysqlSg.securityGroupId),
      ec2.Port.tcp(443),
      'HTTPS from seeding Lambda (joins mysqlSg)',
    );
    secretsEndpointSg.addIngressRule(
      ec2.Peer.securityGroupId(pgSg.securityGroupId),
      ec2.Port.tcp(443),
      'HTTPS from seeding Lambda (joins pgSg)',
    );

    new ec2.InterfaceVpcEndpoint(this, 'SecretsManagerEndpoint', {
      vpc,
      service: ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
      subnets: dbSubnets,
      securityGroups: [secretsEndpointSg],
      privateDnsEnabled: true,
    });

    // 7. Custom Resource via cr.Provider (Req 7.1, 7.2, 7.3, 7.5)
    const provider = new cr.Provider(this, 'SeedingProvider', {
      onEventHandler: seedingLambda,
      logRetention: logs.RetentionDays.ONE_WEEK,
    });

    const seedingResource = new cdk.CustomResource(this, 'SeedingResource', {
      serviceToken: provider.serviceToken,
      resourceType: 'Custom::AgenticDataIngestionTestFixturesSeeding',
      properties: { SeedHash: seedHash },
    });
    // Ensure both DBs reach AVAILABLE before CFN invokes the Custom Resource.
    seedingResource.node.addDependency(mysqlSource);
    seedingResource.node.addDependency(pgTarget);

    // 8. Optional Glue Connections gated by `adi:skipGlueConnections` (Req 8.*)
    const skipGlue =
      this.node.tryGetContext('adi:skipGlueConnections') === 'true';
    let mysqlGlueConnectionName: string | undefined;
    let pgGlueConnectionName: string | undefined;

    if (!skipGlue) {
      // Both connections share dbSubnets.subnets[0] for PhysicalConnectionRequirements.
      // The self-referencing SG ingress (declared above) is what actually grants
      // network reachability — the subnet/AZ here is just metadata Glue uses to
      // place ENIs that join the SG.
      const sharedSubnet = dbSubnets.subnets[0];

      // JDBC_DRIVER_CLASS_NAME is intentionally NOT set here for the same
      // reason it is omitted by `shared/jdbc_helpers.py::create_glue_jdbc_connection`:
      // for built-in engines (MySQL, Postgres, MariaDB, Oracle, SQL Server,
      // Aurora) Glue auto-detects the driver class from the JDBC URL's
      // protocol prefix. When `JDBC_DRIVER_CLASS_NAME` IS set, Glue treats
      // it as a custom-driver hint and demands `JDBC_DRIVER_JAR_URI`
      // alongside — failing CreateCrawler / StartJobRun with
      // "Please provide both the driver s3 path and driver classname".
      const mysqlConn = new glue.CfnConnection(this, 'MySQLGlueConnection', {
        catalogId: this.account,
        connectionInput: {
          name: 'agentic-data-ingestion-test-fixtures-mysql-source',
          connectionType: 'JDBC',
          connectionProperties: {
            JDBC_CONNECTION_URL: `jdbc:mysql://${mysqlSource.dbInstanceEndpointAddress}:${mysqlSource.dbInstanceEndpointPort}/appdb`,
            JDBC_ENFORCE_SSL: 'true',
            // SECRET_ID, not inline USERNAME/PASSWORD. Three reasons, and the
            // first is why this connection was unusable as a pipeline SOURCE:
            //
            //  1. The JDBC source PySpark template calls get_secret_value() at
            //     runtime, so `pipelines_jdbc` refuses a source connection that
            //     carries no SECRET_ID. A fixture with only inline credentials
            //     fails pipeline creation, which is exactly what happened.
            //  2. Glue accepts SECRET_ID as a connection property and rejects it
            //     only *together with* inline USERNAME/PASSWORD — they are
            //     mutually exclusive ("Validation for connection properties
            //     failed"). The note this replaces claimed Glue rejects
            //     SECRET_ID outright, which is wrong; that belief is what put
            //     the credentials inline.
            //  3. Inline credentials resolve at deploy time, so the plaintext
            //     pair is stored on the connection — which the ARN-only
            //     credential invariant forbids.
            SECRET_ID: mysqlSource.secret!.secretArn,
          },
          physicalConnectionRequirements: {
            subnetId: sharedSubnet.subnetId,
            securityGroupIdList: [mysqlSg.securityGroupId],
            availabilityZone: sharedSubnet.availabilityZone,
          },
        },
      });
      mysqlConn.node.addDependency(mysqlSource);
      // No `cdk.Tags.of(...).add('adi:secret_arn', ...)` here any more. It was a
      // no-op twice over: `AWS::Glue::Connection` has no `Tags` property, and
      // Glue rejects `GetTags`/`TagResource` on a connection ARN outright, so
      // nothing could have written or read it. The secret ARN now lives in
      // `SECRET_ID` above, which is the only place any reader looks.
      mysqlGlueConnectionName = 'agentic-data-ingestion-test-fixtures-mysql-source';

      const pgConn = new glue.CfnConnection(this, 'PostgresGlueConnection', {
        catalogId: this.account,
        connectionInput: {
          name: 'agentic-data-ingestion-test-fixtures-postgres-target',
          connectionType: 'JDBC',
          connectionProperties: {
            JDBC_CONNECTION_URL: `jdbc:postgresql://${pgTarget.clusterEndpoint.hostname}:${pgTarget.clusterEndpoint.port}/testdb`,
            JDBC_ENFORCE_SSL: 'true',
            // SECRET_ID rather than inline credentials, for the reasons given on
            // the MySQL connection above. This one is a pipeline TARGET, where
            // inline credentials are tolerated, so the driver here is the third
            // reason rather than the first: plaintext must not sit on the
            // connection, and a target that references its secret keeps the
            // credential out of the Glue job's arguments too.
            SECRET_ID: pgTarget.secret!.secretArn,
          },
          physicalConnectionRequirements: {
            subnetId: sharedSubnet.subnetId,
            securityGroupIdList: [pgSg.securityGroupId],
            availabilityZone: sharedSubnet.availabilityZone,
          },
        },
      });
      pgConn.node.addDependency(pgTarget);
      // Tag the connection with the secret ARN (Glue rejects SECRET_ID in properties)
      // No `adi:secret_arn` tag — see the MySQL connection above for why it
      // could never have worked. The ARN is in `SECRET_ID`.
      pgGlueConnectionName = 'agentic-data-ingestion-test-fixtures-postgres-target';
    }

    // 9. CloudFormation outputs (Req 1.9, 8.7)
    new cdk.CfnOutput(this, 'MySQLSourceEndpoint', {
      value: mysqlSource.dbInstanceEndpointAddress,
      description: 'MySQL source endpoint (host)',
    });
    new cdk.CfnOutput(this, 'MySQLSourceSecretArn', {
      value: mysqlSource.secret!.secretArn,
      description: 'ARN of the Secrets Manager secret for the MySQL source',
    });
    new cdk.CfnOutput(this, 'PostgresTargetEndpoint', {
      value: pgTarget.clusterEndpoint.hostname,
      description: 'Postgres target Aurora cluster writer endpoint (host)',
    });
    new cdk.CfnOutput(this, 'PostgresTargetSecretArn', {
      value: pgTarget.secret!.secretArn,
      description:
        'ARN of the Secrets Manager secret for the Postgres target',
    });
    if (mysqlGlueConnectionName) {
      new cdk.CfnOutput(this, 'MySQLGlueConnectionName', {
        value: mysqlGlueConnectionName,
        description:
          'Glue Connection name for the MySQL source (use in wizard glue_connection mode)',
      });
    }
    if (pgGlueConnectionName) {
      new cdk.CfnOutput(this, 'PostgresGlueConnectionName', {
        value: pgGlueConnectionName,
        description:
          'Glue Connection name for the Postgres target (use in wizard glue_connection mode)',
      });
    }
  }
}
