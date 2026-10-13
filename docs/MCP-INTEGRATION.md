# MCP Integration Documentation

## Overview

Agentic Data Ingestion uses **5 focused MCP servers** deployed in AWS Bedrock AgentCore Runtime. Each server is a separate container with a specific responsibility, providing tools for code generation, infrastructure management, analytics, S3 Tables operations, and legacy data processing capabilities through the Model Context Protocol (MCP).

---

## Table of Contents

1. [Architecture](#architecture)
2. [Data Processing MCP Server](#data-processing-mcp-server)
3. [S3 Tables MCP Server](#s3-tables-mcp-server)
4. [Authentication](#authentication)
5. [Integration Patterns](#integration-patterns)
6. [Deployment](#deployment)
7. [Monitoring](#monitoring)

---

## Architecture

### The focused MCP servers

`mcp/` holds one directory per server plus `vendor/`, the single vendored copy of the AWS Labs
DataProcessing package — **`vendor/` is not a server**, which is the counting rule that matters here.
**Every** server is built: each has its own `Dockerfile` and a `DockerImageAsset` in
`deploy-cdk/lib/agentcore-stack.ts`, and each deploys as a separate AgentCore Runtime container. The
server count, and the derivation carrying that `vendor/` exclusion, are stated once in
[ARCHITECTURE.md § System Inventory](ARCHITECTURE.md#system-inventory); the table below is one row
per server.

| # | Server | Directory | Tools registered | Scope | Consumers |
|---|--------|-----------|------------------|-------|-----------|
| 1 | Codegen | `mcp/codegen_server/` | 8 | Glue ETL jobs, data catalog, interactive sessions | `create_glue_pipeline`'s worker-recommendation agent, via `AGENTCORE_CODEGEN_ENDPOINT`. `create_code` and `test_code` also receive this server's URL, but under the generic name `AGENTCORE_ENDPOINT` (`compute-stack.ts:642`, `:673`), and neither reaches it: `create_code` never reads the variable, and `test_code`'s MCP path is unreachable because `USE_AGENTCORE_RUNTIME='true'` returns at the top of its handler (`lambda/agentic_data_ingestion_test_code/lambda_function.py:3064-3071`), before the factory call at `:3259`. |
| 2 | Infra | `mcp/infra_server/` | 19 | Crawlers, Glue commons, workflows, catalog connections, read-only IAM, S3 | **None.** `create_infrastructure` provisions via boto3 directly. |
| 3 | Analytics | `mcp/analytics_server/` | 8 | Athena queries / catalogs / workgroups, EMR clusters / steps / instances | `create_glue_pipeline`, via `AGENTCORE_ANALYTICS_ENDPOINT`. |
| 4 | S3 Tables | `mcp/s3_tables/` | 10 live, 7 more write-gated | S3 Table Buckets, Iceberg tables | **None.** S3 Tables provisioning happens via boto3 in the Lambdas. |
| 5 | Data Processing | `mcp/data_processing/` | 30 (legacy) | Monolithic predecessor of servers 1–3, retained for reference | **None.** |

**Where these figures come from, and how to re-derive them.** Only `s3_tables` registers its own
tools. The other four construct vendored handler classes from
`mcp/vendor/awslabs_aws_dataprocessing_mcp_server/`, and each handler registers a fixed set, so a
server's surface is the union over the handlers it constructs. Re-derive rather than cite:

```bash
# every tool name registered anywhere under mcp/
grep -rhoE 'tool\(name="[a-zA-Z_0-9]+"' mcp/ --include='*.py' | sed -E 's/.*"(.*)"/\1/' | sort -u
# which handlers a given server constructs
grep -oE '[A-Za-z0-9]+Handler\(' mcp/codegen_server/server.py | sort -u
```

Two subtractions are already applied to the table, so the numbers above are what a deployed runtime
exposes rather than a count of registration sites:

- **IAM mutation is never registered** on infra or data_processing. Both pass
  `register_iam_mutation_tools=False` (`mcp/infra_server/server.py:135`,
  `mcp/data_processing/server.py:190`), which drops `add_inline_policy` and
  `create_data_processing_role` at registration time rather than refusing them at call time
  (P28 R16.2). So infra is 19 of 21 sites and data_processing 30 of 32.
- **S3 Tables mutation is gated on `allow_write`** (`mcp/s3_tables/agentcore_s3tables_mcp.py:98`),
  and every runtime pins `MCP_ALLOW_WRITE: 'false'` (`deploy-cdk/lib/agentcore-stack.ts:107`), so
  the 7 create/rename/delete/put tools are absent from the live tool list, not merely refused.

**Counting rule for the analytics figure.** It is **8**, not the `~15` an earlier design named.
`mcp/analytics_server/server.py:15-26` records why: the CloudWatch-metrics and cost-estimation
handlers do not exist in the codebase, so the server registers only the 8 available Athena + EMR
tools. `mcp/README.md` carries `~10` for codegen and `~17` for infra; both are approximations that
the derivation above supersedes.

### Tool naming: coarse multiplexers, not one tool per operation

The vendored handlers expose **one tool per resource family**, named `manage_aws_<service>_<family>`,
with the verb passed as an `operation` argument. `manage_aws_glue_jobs` is a single tool whose
`operation` accepts `create-job`, `delete-job`, `get-job`, `get-jobs`, `update-job`,
`start-job-run`, `stop-job-run`, `get-job-run`, `get-job-runs`, `batch-stop-job-run`,
`get-job-bookmark` and `reset-job-bookmark`
(`mcp/vendor/awslabs_aws_dataprocessing_mcp_server/awslabs/aws_dataprocessing_mcp_server/handlers/glue/glue_etl_handler.py:65-73`).

This matters when reading the rest of this document. Names like `create_glue_job`,
`run_athena_query` or `start_interactive_session` are **operations**, not tools, and calling one by
that name fails with "tool not found". The write operations inside these multiplexers are refused at
call time while `MCP_ALLOW_WRITE=false`, which is every deployed runtime, so the reachable surface
is the read and describe operations. To see the live list for a runtime, call
`session.list_tools()` against it rather than trusting a name in prose.

**Per-server write and sensitive-data access posture (P28 R16)** → [`mcp/README.md`](../mcp/README.md),
§ *Access flags — least privilege (P28 R16)*. Owned there and referenced from
[`docs/AGENTS.md`](AGENTS.md); deliberately not restated here.

> `mcp/data_processing/` **must not be deleted, but not for the reason previously given here.**
> This note used to say `compute-stack.ts` copies the directory's `streamable_http_sigv4.py` into
> the Strands Lambda layer. That is no longer true: the SigV4 transport lives at
> `shared/streamable_http_sigv4.py`, which is its only tracked copy, and `compute-stack.ts`
> references `data_processing` nowhere.
>
> The live reason is the AgentCore stack. `agentcore-stack.ts:311` builds
> `data_processing/Dockerfile` as a `DockerImageAsset` and `:345` creates a runtime from it, so
> removing the directory fails `cdk synth` on that stack. Retiring the server means removing the
> asset and the runtime first, in the same change.

### Key Benefits

1. **Separation of Concerns**: Each server has a focused responsibility
2. **Independent Scaling**: Servers scale based on their own workload
3. **Targeted Tool Selection**: AI agents connect to the server with the tools they need
4. **Simplified Debugging**: Smaller servers are easier to troubleshoot
5. **Unified Monitoring**: CloudWatch logs per server for targeted analysis

---

## Data Processing MCP Server

### Overview

The Data Processing MCP server is the monolithic predecessor of the codegen, infra and analytics
servers. It constructs all thirteen vendored handlers, which register **30** tools between them, and
it has no consumers. It uses the FastMCP framework with handler-based architecture.

### Registered tools, by handler

One row per handler, listing the tool names it actually registers. Every `manage_aws_*` entry is a
multiplexer taking an `operation` argument, so the per-resource verbs live inside these tools rather
than beside them (see *Tool naming* above).

| Handler | Tools registered |
|---------|------------------|
| GlueDataCatalogHandler | `manage_aws_glue_databases`, `manage_aws_glue_tables`, `manage_aws_glue_connections`, `manage_aws_glue_partitions`, `manage_aws_glue_catalog` |
| GlueEtlJobsHandler | `manage_aws_glue_jobs` |
| GlueInteractiveSessionsHandler | `manage_aws_glue_sessions`, `manage_aws_glue_statements` |
| GlueWorkflowAndTriggerHandler | `manage_aws_glue_workflows`, `manage_aws_glue_triggers` |
| CrawlerHandler | `manage_aws_glue_crawlers`, `manage_aws_glue_classifiers`, `manage_aws_glue_crawler_management` |
| GlueCommonsHandler | `manage_aws_glue_usage_profiles`, `manage_aws_glue_security_configurations`, `manage_aws_glue_encryption`, `manage_aws_glue_resource_policies` |
| AthenaQueryHandler | `manage_aws_athena_query_executions`, `manage_aws_athena_named_queries` |
| AthenaDataCatalogHandler | `manage_aws_athena_data_catalogs`, `manage_aws_athena_databases_and_tables` |
| AthenaWorkGroupHandler | `manage_aws_athena_workgroups` |
| EMREc2ClusterHandler | `manage_aws_emr_clusters` |
| EMREc2StepsHandler | `manage_aws_emr_ec2_steps` |
| EMREc2InstanceHandler | `manage_aws_emr_ec2_instances` |
| CommonResourceHandler | `get_policies_for_role`, `get_roles_for_service`, `list_s3_buckets`, `upload_to_s3`, `analyze_s3_usage_for_data_processing`. The two IAM-mutation tools are **not** registered here (`mcp/data_processing/server.py:190`) |

> **A Glue interactive session IS the live code-testing executor, and an earlier revision of this
> note said the opposite.** Both stacks publish `VERIFICATION_EXECUTOR: 'glue'`
> (`deploy-cdk/lib/compute-stack.ts:724`, `deploy-cdk/lib/agentcore-stack.ts:170`), so `test_code`
> dispatches to `GlueSessionTestRunner` and starts a real Spark session on every run. That is a
> **deliberate reversal** of the earlier pandas-proxy default, recorded at both CDK sites and in
> `lambda/agentic_data_ingestion_test_code/glue_session_runner.py:52-64`.
>
> Two consequences worth carrying, because they are easy to get wrong from the old framing. **Code
> Interpreter is the fallback, not the default** — it runs a PySpark-to-pandas proxy, and
> `ARCHITECTURE.md:1777` is explicit that a transform which passes it has not been run against Spark.
> And because the Glue branch is now reached on every run rather than on opt-in, its refusal paths,
> above all the session-capacity refusal, are **ordinary operating behaviour rather than edge cases**.
>
> What *is* true about the MCP side: this step does not reach Glue through MCP. The session is created
> by `boto3` in the Lambda. `manage_aws_glue_sessions` and `manage_aws_glue_statements` remain
> registered as MCP tools for other uses such as ad-hoc Spark queries, and are not on this path.

> **No cost, sizing or profiling tools exist on any server.** Earlier revisions of this document
> listed `analyze_data_characteristics`, `calculate_optimal_workers`, `estimate_glue_cost`,
> `validate_configuration`, `recommend_transformations` and `generate_test_data` as callable, in
> code examples throughout. None of them is registered anywhere under `mcp/`, and the grep in
> *Where these figures come from* above returns the full list that is. Worker sizing is done by
> `create_glue_pipeline` in Lambda code, reasoning over read-only Glue and Athena describes; there
> is no tool to call for it. `mcp/analytics_server/server.py:15-18` records the same gap for the
> CloudWatch-metrics and cost handlers a design once assumed.

### Usage Example: Code Generation

```python
# Lambda function using an MCP server profile.
# The pip distribution is `strands-agents`; the importable module is `strands`.
# Nothing is installed under the distribution's own name, so an import spelled
# that way raises ImportError. In-tree call sites for the correct form:
# lambda/agentic_data_ingestion_create_code/lambda_function.py:58,
# agentcore/nl_pipeline_builder/main.py:97, shared/strands_mcp_factory.py:276.
from strands import Agent  # noqa: F401  (direct use is for the no-MCP path)
from strands_mcp_factory import initialize_strands_agent_with_mcp

# The factory wires the SigV4 MCP transport and returns (agent, mcp_client);
# it falls back to (None, None) when MCP is unavailable.
# Signature: shared/strands_mcp_factory.py:246-251
agent, mcp_client = initialize_strands_agent_with_mcp(
    system_prompt="You generate optimized PySpark ETL code.",
    endpoint=os.environ['AGENTCORE_ENDPOINT'],
)

# Agent autonomously uses MCP tools
result = agent.run(
    task="Generate optimized PySpark code for customer data transformation",
    context={
        "source_schema": source_schema,
        "target_schema": target_schema,
        "data_volume": "50GB",
        "complexity": "medium"
    }
)

# The tools the agent can actually reach on the codegen profile, all read-only
# while MCP_ALLOW_WRITE=false:
#   manage_aws_glue_tables(operation="get-tables", database_name=...)
#   manage_aws_glue_databases(operation="get-databases")
#   manage_aws_glue_jobs(operation="get-jobs")
#   manage_aws_glue_sessions / manage_aws_glue_statements  (ad-hoc Spark)
# There is no tool that analyses data characteristics or proposes
# transformations; that reasoning is the model's, from the context above.
```

### Usage Example: Glue Job Configuration

Worker sizing has **no backing MCP tool**. `create_glue_pipeline` reaches the codegen and analytics
profiles for read-only context (existing job configurations, table metadata, Athena describes) and
the model produces the recommendation from that context. The recommendation is then applied by
Lambda code through boto3, not through MCP.

```python
# Agent recommends optimal Glue job configuration
result = agent.run(
    task="Recommend optimal Glue job configuration",
    context={
        "data_volume": "100GB",
        "record_count": 5000000,
        "transformation_complexity": "high",
        "target_type": "S3_TABLES"
    }
)

# Context the agent can gather first, all read-only:
#   manage_aws_glue_jobs(operation="get-jobs")            # existing sizings
#   manage_aws_glue_jobs(operation="get-job-runs", job_name=...)
#   manage_aws_athena_databases_and_tables(operation="list-table-metadata", ...)
#
# Returned recommendation, a model judgement over that context, NOT a tool result:
# {
#   "worker_type": "G.2X",
#   "number_of_workers": 10,
#   "timeout_minutes": 60,
#   "estimated_duration_minutes": 45,
#   "rationale": "High complexity transformation with 100GB data..."
# }
#
# Note there is no cost field. Nothing in the MCP surface prices a Glue run,
# so a per-run cost estimate would be a fabricated number.
```

---

## S3 Tables MCP Server

### Overview

The S3 Tables MCP server manages S3 Table Buckets and Apache Iceberg tables through the
`S3TablesHandler` class with FastMCP. It registers 17 tools in source, but **10 of them in any
deployed runtime**: the 7 mutation tools sit behind `if self.allow_write:`
(`mcp/s3_tables/agentcore_s3tables_mcp.py:98`) and every runtime pins `MCP_ALLOW_WRITE: 'false'`
(`deploy-cdk/lib/agentcore-stack.ts:107`). Registration-gating rather than call-time refusal means
the write tools do not appear in `list_tools()` at all, so a client cannot see them to attempt them.

This server has no consumers. S3 Tables provisioning is done by boto3 in the Lambdas.

### Tool Categories

Unmarked tools are always registered. **(write-gated)** marks a tool absent unless
`MCP_ALLOW_WRITE=true`, which no deployment sets.

#### 1. Connectivity (1 tool)
- `test_s3tables_connection` - Test S3 Tables service connectivity

#### 2. S3 Table Bucket Management (3 tools)
- `agentcore_list_s3_table_buckets` - List all buckets
- `agentcore_get_s3_table_bucket` - Get bucket details
- `agentcore_create_s3_table_bucket` - Create S3 Table Bucket **(write-gated)**

#### 3. Namespace Management (2 tools)
- `agentcore_list_s3_table_namespaces` - List namespaces
- `agentcore_create_s3_table_namespace` - Create namespace **(write-gated)**

#### 4. Table Operations (6 tools)
- `agentcore_list_s3_tables` - List tables in namespace
- `agentcore_get_s3_table` - Get table details
- `agentcore_get_s3_table_metadata_location` - Get Iceberg metadata location
- `agentcore_create_s3_table` - Create Iceberg table **(write-gated)**
- `agentcore_rename_s3_table` - Rename table **(write-gated)**
- `agentcore_delete_s3_table` - Delete table **(write-gated)**

#### 5. Policy Management (3 tools)
- `agentcore_get_s3_table_policy` - Get resource policy
- `agentcore_put_s3_table_policy` - Set resource policy **(write-gated)**
- `agentcore_delete_s3_table_policy` - Delete resource policy **(write-gated)**

#### 6. Optimization & Cost (2 tools)
- `agentcore_recommend_iceberg_config` - Recommend optimal Iceberg configuration
- `agentcore_estimate_s3_tables_cost` - Estimate S3 Tables costs

**There are no schema-validation or schema-evolution tools.** Earlier revisions of this document
showed `validate_schema`, `evolve_schema`, `get_schema_history` and `optimize_iceberg_config` in
examples; none is registered. Iceberg schema evolution is not reachable through this server at all.
The names in the list above are the complete surface, and they are all prefixed `agentcore_` apart
from `test_s3tables_connection`, so an unprefixed spelling such as `create_table` is not a shorthand,
it is a call that fails.

### Usage Example: Infrastructure Provisioning

```python
# Lambda function using S3 Tables MCP
result = agent.run(
    task="Create S3 Table Bucket with Iceberg table",
    context={
        "bucket_name": "customer-data-lake",
        "table_name": "customers",
        "namespace": "default",
        "schema": target_schema,
        "partition_keys": ["year", "month"]
    }
)

# This example is ILLUSTRATIVE OF A SHAPE, not a working call sequence.
# No Lambda provisions S3 Tables through MCP, and the three creation tools it
# would need are unregistered while MCP_ALLOW_WRITE=false:
#   agentcore_create_s3_table_bucket   (write-gated)
#   agentcore_create_s3_table_namespace (write-gated)
#   agentcore_create_s3_table          (write-gated)
# Reachable today, read-only:
#   agentcore_list_s3_table_buckets()
#   agentcore_get_s3_table_bucket(...)
#   agentcore_get_s3_table_metadata_location(...)
#   agentcore_recommend_iceberg_config(...)
# The real provisioning path is boto3 in create_infrastructure. To make the
# sequence above work, MCP_ALLOW_WRITE would have to be flipped on this one
# runtime in agentcore-stack.ts, with a recorded consumer and rationale.

# Returns:
# {
#   "bucket_name": "customer-data-lake",
#   "table_name": "customers",
#   "data_location": "s3://customer-data-lake/tables/default/customers/data/",
#   "metadata_location": "s3://customer-data-lake/tables/default/customers/metadata/",
#   "schema_version": 1
# }
```

### Schema evolution is not supported by this server

There is no MCP path for Iceberg schema evolution. A previous revision of this document showed a
`get_table` / `validate_schema` / `evolve_schema` / `get_schema_history` sequence here; none of those
four tools exists on any server, so the example has been removed rather than corrected. The closest
reachable operations are read-only inspection, `agentcore_get_s3_table` and
`agentcore_get_s3_table_metadata_location`. Altering a table's schema means boto3 or an Iceberg
client in Lambda or Glue code.

---

## Authentication

### AWS SigV4 Authentication

All MCP communication uses AWS Signature Version 4 (SigV4) authentication.

#### Implementation

```python
# streamable_http_sigv4.py
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import boto3

class StreamableHTTPSigV4:
    def __init__(self, service='bedrock-agentcore', region=None):
        self.service = service
        self.region = region or os.environ.get('AWS_REGION', 'us-west-2')
        self.credentials = boto3.Session().get_credentials()
    
    def sign_request(self, method, url, headers, body):
        request = AWSRequest(
            method=method,
            url=url,
            headers=headers,
            data=body
        )
        
        SigV4Auth(self.credentials, self.service, self.region).add_auth(request)
        
        return dict(request.headers)
```

#### Usage in Lambda

```python
# Lambda function
from streamable_http_sigv4 import StreamableHTTPSigV4
from mcp import ClientSession

# Initialize SigV4 auth
auth = StreamableHTTPSigV4(
    service='bedrock-agentcore',
    region=os.environ['AWS_REGION']
)

# Connect to AgentCore MCP
async with ClientSession(
    read_stream=read_stream,
    write_stream=write_stream,
    auth=auth
) as session:
    # List available tools
    tools = await session.list_tools()
    
    # Call tool
    result = await session.call_tool(
        name="manage_aws_glue_databases",
        arguments={"operation": "get-databases"}
    )
```

### IAM Permissions

Lambda execution role requires:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:InvokeRuntime",
        "bedrock-agentcore:StartSession",
        "bedrock-agentcore:StopSession",
        "bedrock-agentcore:ExecuteCode"
      ],
      "Resource": "arn:aws:bedrock-agentcore:*:*:runtime/*"
    }
  ]
}
```

---

## Integration Patterns

### Pattern 1: Code Generation with Dual MCP

```python
# Step 5: Code Generation
async def generate_code(pipeline_id, mappings, source_config, target_config):
    agent = initialize_strands_agent_with_mcp()
    
    # Agent uses both MCP servers
    result = await agent.run(
        task="Generate optimized PySpark ETL code",
        context={
            "mappings": mappings,
            "source_config": source_config,
            "target_config": target_config
        }
    )
    
    # Codegen MCP tools available for context, all read-only:
    # - manage_aws_glue_tables(operation="get-table", ...)
    # - manage_aws_glue_databases(operation="get-databases")
    # - manage_aws_glue_jobs(operation="get-jobs")
    #
    # "Dual MCP" is aspirational here: only the codegen and analytics profiles
    # are wired to a Lambda, and this step reaches the codegen one. The S3
    # Tables server has no consumer, and the schema-validation and
    # Iceberg-optimisation tools this pattern once named do not exist.
    
    return result
```

### Pattern 2: Testing with Code Interpreter (Phase 12)

> **Updated**: Code testing no longer uses MCP Interactive Sessions. It uses AgentCore Code Interpreter for sandboxed pandas execution.

```python
# Step 6: Code Testing — AgentCore Code Interpreter
from code_interpreter_runner import CodeInterpreterTestRunner

def test_code(transform_block, source_schema, target_schema):
    # run_test takes the RAW transform block. It converts PySpark to pandas
    # itself, before a session is started, so an unconvertible construct
    # never costs a session.
    runner = CodeInterpreterTestRunner(region='us-west-2')
    results = runner.run_test(transform_block, source_schema, target_schema)

    # Returns real metrics:
    # {
    #   "execution_successful": True,
    #   "executed": True,          # did code actually run in the interpreter
    #   "exit_code": 0,
    #   "stderr": "",
    #   "input_records": 500,
    #   "output_records": 500,
    #   "data_quality_results": {
    #       "completeness_score": 95,
    #       "uniqueness_score": 100,
    #       "schema_compliance": 100,
    #       "overall_quality_score": 98,
    #       "null_counts": {"name": 25, "age": 12},
    #       "duplicates": 0,
    #       "missing_columns": [],
    #   },
    #   "error_details": None,
    #   "conversion_report": {"input_lines": 5, "output_lines": 5,
    #                         "constructs_converted": ["col", "trim", "withColumn"]},
    # }
    return results
```

If the converter refuses the block, `run_test` returns a structured
**non-verification** rather than raising: `executed` is `False`, `exit_code` and
`conversion_report` are `None`, and the result additionally carries
`not_verified_by_proxy`, `not_verified_reason`, `not_verified_construct` and
`not_verified_line_number`.
The transform never ran, so it is neither passed nor failed. Calling
`PySpark2PandasConverter.convert` directly is only for tooling that wants the
converted code — it is a classmethod returning
`{"code": <str>, "conversion_report": {...}}`, and it raises `ConversionError`
on a construct it cannot express. See
[`pyspark_templates/README.md`](../pyspark_templates/README.md#what-the-pandas-proxy-converts)
for the construct vocabulary.

**Why the change**: MCP Interactive Sessions opened 7 separate SigV4 connections per test, took 2-5 minutes for Glue session startup, and returned generic responses with all metrics at 0. Code Interpreter completes in ~10-15 seconds with real metrics.

### Pattern 3: Infrastructure Provisioning

```python
# Step 7: Infrastructure Provisioning
async def provision_infrastructure(pipeline_id, target_config):
    if target_config['type'] != 'NEW_S3_TABLE_BUCKET':
        # Skip for existing targets
        return {"infrastructure_created": False}
    
    agent = initialize_strands_agent_with_mcp()
    
    # Agent uses S3 Tables MCP
    result = await agent.run(
        task="Create S3 Table Bucket with Iceberg table",
        context={
            "bucket_name": target_config['s3_table_bucket'],
            "table_name": target_config['table_name'],
            "schema": target_config['schema']
        }
    )
    
    # ILLUSTRATIVE SHAPE ONLY. This is not how provisioning works today.
    # create_infrastructure provisions S3 Tables with boto3 directly and holds
    # no S3 Tables MCP endpoint. The creation tools are unregistered while
    # MCP_ALLOW_WRITE=false, and no schema-validation tool exists on any server.
    # See "S3 Tables MCP Server" above for the 10 tools actually reachable.
    
    return result
```

### Pattern 4: Glue Job Creation with Optimization

```python
# Step 8: Glue Job Creation
async def create_glue_job(pipeline_id, code_version_id, target_config):
    agent = initialize_strands_agent_with_mcp()
    
    # Agent uses both MCP servers
    result = await agent.run(
        task="Create optimized Glue job",
        context={
            "pipeline_id": pipeline_id,
            "code_version_id": code_version_id,
            "target_config": target_config,
            "data_volume": "50GB"
        }
    )
    
    # Read-only context the agent gathers, on the codegen and analytics profiles:
    # - manage_aws_glue_jobs(operation="get-jobs")
    # - manage_aws_glue_jobs(operation="get-job-runs", job_name=...)
    # - manage_aws_athena_databases_and_tables(operation="list-table-metadata", ...)
    #
    # The job itself is NOT created through MCP. manage_aws_glue_jobs supports
    # operation="create-job", but that operation is refused while
    # MCP_ALLOW_WRITE=false, so create_glue_pipeline calls Glue CreateJob with
    # boto3 under the Lambda's own IAM role. Sizing is a model judgement over
    # the context above; no tool computes workers or prices a run.
    
    return result
```

---

## Deployment

### Docker Image Build

```bash
# Build Data Processing MCP image
cd mcp/data_processing
docker build -t agentic-data-ingestion-data-processing-mcp:latest .

# Build S3 Tables MCP image
cd mcp/s3_tables
docker build -t agentic-data-ingestion-s3tables-mcp:latest .
```

### Push to ECR

```bash
# Authenticate to ECR
aws ecr get-login-password --region us-west-2 | \
  docker login --username AWS --password-stdin \
  123456789012.dkr.ecr.us-west-2.amazonaws.com

# Tag and push Data Processing MCP
docker tag agentic-data-ingestion-data-processing-mcp:latest \
  123456789012.dkr.ecr.us-west-2.amazonaws.com/agentic-data-ingestion-mcp-server:dataprocessing-dev
docker push 123456789012.dkr.ecr.us-west-2.amazonaws.com/agentic-data-ingestion-mcp-server:dataprocessing-dev

# Tag and push S3 Tables MCP
docker tag agentic-data-ingestion-s3tables-mcp:latest \
  123456789012.dkr.ecr.us-west-2.amazonaws.com/agentic-data-ingestion-mcp-server:s3tables-dev
docker push 123456789012.dkr.ecr.us-west-2.amazonaws.com/agentic-data-ingestion-mcp-server:s3tables-dev
```

### Deploy AgentCore Runtime

All AgentCore resources are deployed via CDK:

```bash
cd deploy-cdk
npx cdk deploy AgenticDataIngestion-AgentCore-dev --require-approval never
```

The `agentcore-stack.ts` handles Docker image builds, ECR push, and AgentCore Runtime resource creation automatically.

### Environment Variables

Lambda functions need these environment variables:

```bash
# AgentCore MCP endpoint
AGENTCORE_ENDPOINT=https://bedrock-agentcore-runtime.us-west-2.amazonaws.com/runtime/abc123

# Enable/disable MCP
MCP_ENABLED=true

# AWS region
AWS_REGION=us-west-2
```

---

## Monitoring

### CloudWatch Metrics

Monitor AgentCore Runtime metrics:
- `InvocationCount` - Total MCP tool invocations
- `InvocationErrors` - Failed tool calls
- `InvocationDuration` - Tool execution time
- `ThrottledInvocations` - Rate limit hits

### CloudWatch Logs

AgentCore Runtime logs:
- `/aws/bedrock-agentcore/runtime/{runtime-id}`
- Tool invocation logs
- Error details
- Performance metrics

### Lambda Logs

Lambda function logs include:
- MCP tool selection
- Tool arguments
- Tool responses
- Agent rationale
- Execution time

### Example Log Entry

```json
{
  "timestamp": "2024-11-12T10:30:45.123Z",
  "level": "INFO",
  "message": "MCP tool invocation",
  "tool_name": "manage_aws_glue_jobs",
  "arguments": {
    "operation": "get-job",
    "job_name": "agentic-data-ingestion-pipe-123-etl"
  },
  "response": {
    "job_name": "agentic-data-ingestion-pipe-123-etl",
    "worker_type": "G.2X",
    "number_of_workers": 8
  },
  "execution_time_ms": 234
}
```

### Alarms

Set CloudWatch alarms for:
- MCP invocation errors > 5%
- MCP invocation duration > 30 seconds
- Throttled invocations > 0
- Lambda function errors > 5%

---

## Troubleshooting

### Common Issues

#### 1. MCP Connection Timeout

**Symptom**: Lambda function times out connecting to AgentCore

**Solution**:
- Check `AGENTCORE_ENDPOINT` environment variable
- Verify IAM permissions
- Check VPC configuration (if Lambda in VPC)
- Increase Lambda timeout

#### 2. Tool Not Found

**Symptom**: Agent reports tool not available

**Solution**:
- List available tools: `session.list_tools()`. This is authoritative; prose is not.
- Check you are not calling an *operation* as if it were a tool. `create_glue_job`,
  `run_athena_query` and `start_interactive_session` are operation values passed to
  `manage_aws_glue_jobs`, `manage_aws_athena_query_executions` and `manage_aws_glue_sessions`
  respectively. See *Tool naming* under Architecture.
- Check the tool is not write-gated. The S3 Tables mutation tools are unregistered unless
  `MCP_ALLOW_WRITE=true`, so they are missing from `list_tools()` rather than failing on call.
- Check you are on the right server profile. Athena and EMR tools are on analytics, not codegen.
- Check MCP server deployment and AgentCore Runtime status.

#### 3. Authentication Failure

**Symptom**: 403 Forbidden error

**Solution**:
- Verify IAM role has `bedrock-agentcore:InvokeRuntime` permission
- Check AWS credentials are valid
- Verify SigV4 signing implementation
- Check region matches AgentCore Runtime region

#### 4. Tool Execution Failure

**Symptom**: Tool returns error

**Solution**:
- Check tool arguments format
- Verify AWS service permissions (Glue, S3, etc.)
- Check CloudWatch logs for details
- Validate input data

---

## Best Practices

### 1. Error Handling

Always handle MCP errors gracefully:

```python
try:
    result = await agent.run(task="...", context={...})
except MCPConnectionError:
    # Fall back to non-MCP implementation
    result = fallback_implementation()
except MCPToolError as e:
    # Log error and retry
    logger.error(f"MCP tool error: {e}")
    result = retry_with_different_tool()
```

### 2. Tool Selection

Let the agent select tools autonomously:

```python
# Good: Let agent choose tools
result = await agent.run(
    task="Optimize Glue job configuration",
    context={"data_volume": "50GB"}
)

# Bad: Manually specify tools
result = await session.call_tool(
    name="manage_aws_glue_jobs",
    arguments={"operation": "get-jobs"}
)
```

### 3. Logging

Log MCP tool usage for debugging:

```python
logger.info(f"Agent used MCP tools: {result.tools_used}")
logger.info(f"Agent rationale: {result.rationale}")
logger.info(f"Execution time: {result.execution_time_ms}ms")
```

### 4. Cost Optimization

Monitor MCP usage to optimize costs:
- Use appropriate Bedrock model (Claude vs Nova)
- Cache tool results when possible
- Batch tool calls when appropriate
- Set reasonable timeouts

---

## Related Documentation

- [Architecture Overview](ARCHITECTURE.md)
- [Lambda Functions](LAMBDA-FUNCTIONS.md)
- [Workflow](WORKFLOW.md)
- [Troubleshooting](TROUBLESHOOTING.md)

---

## Phase 12: AgentCore Platform Services

### AgentCore Memory for Schema Mapping

The schema mapping Lambda uses AgentCore Memory to learn from previous mapping decisions:

- **Semantic Memory**: Stores field mapping patterns as searchable text records (e.g., "Field 'cust_id' maps to 'customer_id' with 95% confidence")
- **Event Memory**: Stores temporal audit trail of mapping decisions with timestamps
- **Query**: Before calling Bedrock, queries Memory for similar past mappings using `retrieve_memory_records` with semantic search
- **Feedback loop**: Approval/rejection decisions are stored back in Memory via the approval handler

Feature flag: `MEMORY_ENABLED=false` disables all Memory calls.

### AgentCore Runtime for Agent Lambdas

Four agent Lambdas route through AgentCore Runtime containers. The flag reads like an opt-in, but
CDK sets it to `'true'` for all four unconditionally, so the proxy path is the deployed behaviour and
the in-Lambda agent code below it does not run:

| Lambda | Container | Flag, as deployed |
|--------|-----------|-------------------|
| `create_code` | `agentcore/create_code/` | `USE_AGENTCORE_RUNTIME: 'true'` (`compute-stack.ts:647`) |
| `test_code` | `agentcore/test_code/` | `USE_AGENTCORE_RUNTIME: 'true'` (`compute-stack.ts:679`) |
| `create_glue_pipeline` | `agentcore/create_glue_pipeline/` | `USE_AGENTCORE_RUNTIME: 'true'` (`compute-stack.ts:822`) |
| `create_infrastructure` | `agentcore/create_infrastructure/` | `USE_AGENTCORE_RUNTIME: 'true'` (`compute-stack.ts:843`) |

This is why the inline MCP call in `test_code` never executes: its handler returns at the proxy
branch before reaching it. Reading the flag as "optional" is how a reader concludes an in-Lambda code
path is live when it is not.

Benefits: No cold start, no 15-minute timeout, MCP connection reuse across invocations.

The proxy pattern uses `shared/agentcore_proxy.py` with SigV4-signed HTTP POST to the Runtime endpoint.
