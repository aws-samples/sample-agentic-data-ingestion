# JDBC Test Fixtures Stack

Operator guide for the opt-in `AgenticDataIngestion-TestFixtures-dev` CDK stack.

## What this is

An opt-in seventh CDK stack — `AgenticDataIngestion-TestFixtures-dev` — that provisions a MySQL 8.0 source database, an Aurora Postgres 15 target cluster, two Secrets Manager secrets, a Lambda-backed CloudFormation Custom Resource that idempotently seeds the MySQL source, and (by default) two pre-created Glue Connections. The stack is gated behind the `adi:deployTestFixtures=true` CDK context flag, so a default `cdk deploy --all` skips it. Both databases attach to the existing Agentic Data Ingestion VPC `<your-vpc-id>` and its private-with-egress subnets — no new VPC is created.

## Deploy

```bash
make test-fixtures-deploy
```

Or run the raw CDK command from `agentic_data_ingestion/deploy-cdk/`:

```bash
cd deploy-cdk && npx cdk deploy AgenticDataIngestion-TestFixtures-dev \
  --context adi:deployTestFixtures=true \
  --require-approval never
```

First deploy takes approximately 8–12 minutes; RDS instance creation dominates the wall clock.

## Destroy

```bash
make test-fixtures-destroy
```

Or run the raw CDK command:

```bash
cd deploy-cdk && npx cdk destroy AgenticDataIngestion-TestFixtures-dev \
  --context adi:deployTestFixtures=true \
  --force
```

Destroy takes approximately 15 minutes. The wait is dominated by VPC-attached Lambda hyperplane ENI tear-down — a known AWS behavior, not an Agentic Data Ingestion bug. See [Lambda networking foundations](https://docs.aws.amazon.com/lambda/latest/dg/foundation-networking.html#foundation-nw-eni) for details.

## Status check

```bash
make test-fixtures-status
```

Returns the CloudFormation stack status (e.g. `CREATE_COMPLETE`, `UPDATE_IN_PROGRESS`) or the literal `NOT_DEPLOYED` if the stack does not exist.

## Cost expectations

| Resource | Approx. cost (us-west-2) | Notes |
|---|---|---|
| MySQL_Source (`db.t4g.micro`, 20 GB gp3) | ≈ $13/month | Runs 24/7; small enough to leave running between sessions |
| Postgres_Target (Aurora, single `db.t4g.medium` writer) | ≈ $50/month running, $0/month stopped | Stop the cluster from the RDS console between ad-hoc test sessions |
| **Total (24/7)** | **≈ $63/month** | |

For ad-hoc testing, stop the Postgres cluster between sessions and leave MySQL running.

## Seeded MySQL schema

The seeding Lambda runs `tests/fixtures/seed_sql/mysql.sql` against the `appdb` database on the MySQL source. Detect Schema and the runtime crawler will see the following two tables.

### `customers` — 50 rows

| Column | Type | Notes |
|---|---|---|
| `id` | `INT` | Primary key, 1..50 |
| `email` | `VARCHAR(255)` | Unique |
| `name` | `VARCHAR(255)` | |
| `created_at` | `TIMESTAMP` | Spans the prior 365 days from anchor 2025-01-31 |
| `region` | `VARCHAR(64)` | One of `{us-west, us-east, eu-west, ap-south}` |

### `orders` — 150 rows

| Column | Type | Notes |
|---|---|---|
| `id` | `INT` | Primary key, 1..150 |
| `customer_id` | `INT` | Foreign key → `customers.id` |
| `order_date` | `TIMESTAMP` | Spans the prior 90 days from anchor 2025-01-31 |
| `total_amount` | `DECIMAL(10,2)` | Range [10.00, 500.00] |
| `status` | `VARCHAR(32)` | One of `{pending, shipped, delivered, cancelled}` |

## Empty Postgres `testdb`

The Aurora cluster's `defaultDatabaseName: testdb` setting creates an empty `testdb` database at cluster-creation time. The seeding Lambda runs only a `SELECT 1` health check against it — no DDL, no DML. Application tables are created at runtime by the Glue ETL job's first write.

## CloudFormation outputs

| Output | Purpose |
|---|---|
| `MySQLSourceEndpoint` | MySQL hostname; use as the JDBC URL host |
| `MySQLSourceSecretArn` | Secrets Manager ARN holding `{username, password, host, port, dbname}` |
| `PostgresTargetEndpoint` | Aurora cluster writer hostname |
| `PostgresTargetSecretArn` | Secrets Manager ARN for the Postgres target |
| `MySQLGlueConnectionName` | `agentic-data-ingestion-test-fixtures-mysql-source` — use in the wizard's `glue_connection` mode dropdown. Only emitted when `adi:skipGlueConnections` is NOT `true`. |
| `PostgresGlueConnectionName` | `agentic-data-ingestion-test-fixtures-postgres-target` — same conditional as above |

## Glue Connections opt-out

Pass `--context adi:skipGlueConnections=true` to skip the two Glue Connection resources. The two databases and the seeding Lambda are still created. Use this for accounts that hit the Glue Connection quota.

```bash
cd deploy-cdk && npx cdk deploy AgenticDataIngestion-TestFixtures-dev \
  --context adi:deployTestFixtures=true \
  --context adi:skipGlueConnections=true \
  --require-approval never
```

## Common errors and recovery

### `AlreadyExistsException` on Glue Connection

A previous deploy partially failed and left a connection behind. Delete the stale connections and redeploy:

```bash
aws glue delete-connection --name agentic-data-ingestion-test-fixtures-mysql-source
aws glue delete-connection --name agentic-data-ingestion-test-fixtures-postgres-target
make test-fixtures-deploy
```

### `Stack ... not found in this app` on `cdk destroy`

The Makefile target sets `--context adi:deployTestFixtures=true` automatically. If you run raw `cdk destroy` without it, the entrypoint conditional skips the stack and CDK reports it as missing. Include the flag:

```bash
cd deploy-cdk && npx cdk destroy AgenticDataIngestion-TestFixtures-dev \
  --context adi:deployTestFixtures=true \
  --force
```

## S3 VPC Gateway Endpoint pre-condition

The existing P17 setup added an S3 VPC Gateway Endpoint to the VPC's route tables. If that endpoint is missing, the runtime crawler and Glue ETL job will silently hang on S3 access from inside the VPC — no error, just timeout. Verify it exists before troubleshooting connectivity:

```bash
aws ec2 describe-vpc-endpoints \
  --filters Name=vpc-id,Values=<your-vpc-id> \
            Name=vpc-endpoint-type,Values=Gateway
```

The output should include an endpoint whose `ServiceName` ends in `.s3`.
