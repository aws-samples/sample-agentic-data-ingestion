# cdk-nag Suppression Inventory

AwsSolutionsChecks runs on every synth (disable locally with `-c adi:skipNag=true`).
Findings are either fixed or acknowledged with a written justification. This file is
generated together with `deploy-cdk/lib/nag-acknowledgments.ts` by
`scripts/generate_nag_acknowledgments.py` — regenerate both when findings change:

```bash
cd deploy-cdk && rm -rf cdk.out
npx cdk synth --all --quiet > /tmp/synth.log 2>&1 || true
python3 ../scripts/generate_nag_acknowledgments.py /tmp/synth.log
# review the regenerated lib/nag-acknowledgments.ts + this file, then re-synth to verify exit 0
```

## Provenance and the known gap (hand-written, not generated)

Everything from `## Fixed (not suppressed)` down is generated. This section is not, and a
regeneration discards it along with the rest — see the procedure above.

**Regenerated on 2026-10-13**, from a synth of 349 findings taken with the acknowledgments off,
after the security review fixes. No granular entry was added or removed (115 before and after).
The WebSocket authorizer's new role (S2) raises the same `IAM4` AWSLambdaBasicExecutionRole finding
that already had an entry, so it only joined that row's resource list; and the Infrastructure role,
no longer carrying the authorizer's SSM grant, fits in one policy again, so its `@connections` row
now names `Infrastructure/DefaultPolicy` instead of `OverflowPolicy1`. Three whole-rule
acknowledgments in the bin app changed with it: `CFR3` is **fixed** (CloudFront access logs, S9)
and its acknowledgment removed; `APIG1` now covers only the WebSocket stage, because the REST stage
has access logs; and `COG2` is reworded because MFA is now required by default (S3). The
generator's hard-coded whole-rule list and fixed table were updated to match.

The previous regeneration, **2026-10-12**, was from a synth of 348 findings taken with the acknowledgments
off, after IAM Access Analyzer `ValidatePolicy` (run for the publication review) reported 18
action names in `foundation-stack.ts` that do not exist in IAM. Two of them, `emr:Describe*`
and `emr:List*` on `AgentCorePolicy` (the EMR prefix is `elasticmapreduce`, and no Lambda calls
EMR), each carried an IAM5 acknowledgment here; dropping the actions dropped the findings, so
the regeneration removed exactly those two entries and added none. The other sixteen
(`s3:HeadObject`, `s3:HeadBucket`, `s3:SelectObjectContent`, the `…BucketNotificationConfiguration`
pair, `bedrock-agent:InvokeAgent`, `bedrock-runtime:InvokeModel`, `bedrock-agentcore:CreateMemorySession`)
were never acknowledged findings: a literal action never trips IAM5. The `athena:*` row's
justification still says "EMR access is read-only"; that sentence is generator text and now
describes a grant that no longer exists on this policy.

The previous regeneration, **2026-10-11**, was from 350 findings taken with the acknowledgments
off, after the run-cancel and Glue-state-updater fixes added three prefix-scoped grants: the
dashboard API and job_completion roles gained `states:DescribeExecution`/`StopExecution` on
`execution:agentic-data-ingestion-*:*` (the stateMachine-ARN grant they already carried does not
match an execution), the dashboard API role gained `glue:BatchStopJobRun` on both job-name forms,
and `GlueJobStateUpdateRole` gained `glue:GetJob` on the kebab job form. All three matched
existing generator rules (the Step Functions rule already covered `execution:`; the Glue rule
covers both job forms), so the regeneration added exactly three entries and removed none. A
normal synth had failed with those three unacknowledged, which is how they were found.

The previous regeneration, **2026-10-07**, was from 342 findings after the per-runtime role split
(threat model R-02) changed which roles carry which grants. That closed the gap this section used
to record: the inventory and its generator agree, every row describes a finding that fires, and
rows for grants that no longer exist (the shared runtime role's unconditioned IAM grant, its
secret reads) are gone. The `GlueJobRoleBoundary` entries that were first written into the
generated module by hand come from generator rules.

**The counting rule.** Any count over this file has to say what it counts. Every figure below is
scoped to the **generated** portion, from `## Fixed (not suppressed)` down, because this
hand-written section names rule ids in prose. Counted that way, occurrences of
`AwsSolutions-<rule>` total **118**: 109 IAM5, 6 IAM4, 1 SF1, 1 S10, 1 CFR3. The SF1, S10 and CFR3
occurrences are rows in `## Fixed (not suppressed)`, so the number of **acknowledgment rows is
115**, which equals the generator's **115** entries (109 IAM5, 6 IAM4). On 2026-10-12 these read
117 / 115 / 115, the +1 being CFR3 moving to the fixed table; on 2026-10-11 they read 119 / 117 /
117 (111 IAM5), the −2 being the `emr:*` entries; on 2026-10-07 they read 116 / 114 / 114
(108 IAM5). Re-measure rather than cite; run from
`agentic_data_ingestion/`:

```bash
# generated portion only
sed -n '/^## Fixed (not suppressed)/,$p' docs/CDK-NAG.md \
  | grep -o 'AwsSolutions-[A-Za-z0-9]*' | wc -l
# acknowledgment rows
grep -c '^| `AwsSolutions-' docs/CDK-NAG.md
# generator entries
grep -o 'AwsSolutions-[A-Za-z0-9]*' deploy-cdk/lib/nag-acknowledgments.ts | wc -l
```

**`AwsSolutions-CFR2` is fixed, not acknowledged.** CFR2 (CloudFront distributions require WAF
integration) appears **zero** times in this file and in no acknowledgment anywhere under
`deploy-cdk/`. A CLOUDFRONT-scope WAF is attached to the distribution at
`deploy-cdk/lib/hosting-stack.ts:293` (`webAclId: props.cloudFrontWebAclArn`), sourced from the
EdgeWaf stack in us-east-1 by cross-region reference. The fix is conditional on that stack: the
prop is optional (`hosting-stack.ts:94`) and, as the comment beside it states, no WAF is
attached when it is undefined. The bin app's CFR1 justification leans on the same WAF.

**The `data`, `auth` and `edgeWaf` stacks produced no acknowledged findings.** All eight stacks
are passed to `applyNagAcknowledgments()` (`bin/agentic_data_ingestion.ts:262-274`) and all
eight are declared on `NagAcknowledgmentStacks`, but the function body carries acknowledgment
blocks for only five: `foundation`, `agentcore`, `compute`, `orchestration`, `hosting`
(`deploy-cdk/lib/nag-acknowledgments.ts:26`, `:199`, `:210`, `:239`, `:264`). `data`, `auth`
and `edgeWaf` have no block and no `###` section below — nothing to acknowledge, not an
omission from the inventory.

**IAM4 is not a whole-rule acknowledgment; read the list below as 12 rules, not 13.** The rule
list in `## Whole-rule acknowledgments` opens `IAM4*, L1, S1, …` and its own asterisked
footnote then explains that IAM4 cannot be matched that way, so the list contradicts its
footnote. The bin app makes 12 `Validations.of(app).acknowledge(...)` calls and IAM4 is not
among them: DDB3, CFR1, L1, S1, SQS3, APIG1, APIG2, APIG4, CFR4, COG2, COG8, SMG4. (CFR3 was
the thirteenth until 2026-10-13, when CloudFront access logging fixed it.)
`bin/agentic_data_ingestion.ts:33-38` says so outright — IAM4/IAM5 "are NOT acknowledged here",
because they emit granular per-finding ids that a whole-rule acknowledgment can never match and
that `Validations.acknowledge()` rejects outright. The list is emitted verbatim by
`scripts/generate_nag_acknowledgments.py`, in the `d.append` block that writes this document's
*Whole-rule acknowledgments* heading, so it cannot be corrected in this file alone; this
statement is the correction.

## Fixed (not suppressed)

| Rule | Resource | Fix |
|------|----------|-----|
| AwsSolutions-SF1 | Orchestration state machine | Log level raised to `ALL` (retention capped at 1 month) |
| AwsSolutions-S10 / SQS4 / SNS3 / SF2 | buckets, queues, topics, state machine | `enforceSSL` / TLS-only policies / X-Ray tracing (pre-existing fixes) |
| AwsSolutions-CFR3 | Hosting distribution | Access logs to a private, TLS-only, 30-day bucket outside the `adi-*` grant family (security review S9) |

## Whole-rule acknowledgments (app level, `bin/agentic_data_ingestion.ts`)

Suppressed via `Validations.of(app).acknowledge(...)` — see the justification strings
in the bin app. Rules: IAM4*, L1, S1, SQS3, APIG1, APIG2, APIG4, CFR1, CFR4,
COG2, COG8, SMG4, DDB3.

*IAM4/IAM5 emit granular per-finding IDs that whole-rule acknowledgments cannot match;
those are covered by the generated per-stack acknowledgments below.

## Granular acknowledgments (per stack, generated)

IDs are parameterized: `${account}`/`${region}`/`${environment}` resolve per deployment.

### foundation

| Finding ID | Justification | Resources |
|------------|---------------|-----------|
| `AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]` | AWS managed AWSLambdaBasicExecutionRole provides baseline CloudWatch logging for Lambda functions; acceptable for a sample — replace with customer-managed equivalents in stricter environments. | AgentDispatch/Resource; ApprovalConfig/Resource; BucketNotificationsHandler050a0587b7544547bf325f094a3db834/Role/Resource (+13 more) |
| `AwsSolutions-IAM5[Action::athena:*]` | Analytics MCP server read/query surface; Athena results land in scoped buckets and EMR access is read-only. | AgentCorePolicy/Resource |
| `AwsSolutions-IAM5[Action::glue:*]` | Glue lifecycle admin for runtime-created pipeline resources; the same statement prefix-scopes resources to agentic_data_ingestion*/agentic-data-ingestion-*. | CreateGluePipelineRuntimeRole/DefaultPolicy/Resource; GluePipeline/DefaultPolicy/Resource; QualityChecks/DefaultPolicy/Resource (+1 more) |
| `AwsSolutions-IAM5[Action::glue:BatchGet*]` | Read-only Glue discovery; the same statement prefix-scopes resources to project naming forms. | McpServerRuntimeRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Action::glue:Get*]` | Read-only Glue discovery; the same statement prefix-scopes resources to project naming forms. | AgentCorePolicy/Resource; McpServerRuntimeRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Action::glue:List*]` | Read-only Glue discovery; the same statement prefix-scopes resources to project naming forms. | AgentCorePolicy/Resource; McpServerRuntimeRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Action::s3:GetBucket*]` | CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets. | PipelineTrigger/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Action::s3:GetObject*]` | CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets. | PipelineTrigger/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Action::s3:List*]` | CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets. | PipelineTrigger/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Action::s3tables:*]` | S3 Tables (Iceberg) tables are created per pipeline at runtime; the API is namespaced per table bucket so actions cannot be enumerated at deploy time. | AgentCorePolicy/Resource; CreateInfrastructureRuntimeRole/DefaultPolicy/Resource; Infrastructure/DefaultPolicy/Resource (+1 more) |
| `AwsSolutions-IAM5[Action::s3tables:Get*]` | Read-only S3 Tables discovery for the MCP server runtimes, which run with write access disabled; resources are the account's table buckets. | McpServerRuntimeRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Action::s3tables:List*]` | Read-only S3 Tables discovery for the MCP server runtimes, which run with write access disabled; resources are the account's table buckets. | McpServerRuntimeRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::*]` | The actions in this statement (X-Ray tracing, CloudWatch PutMetricData, Bedrock model invocation, ECR auth token, Step Functions SendTaskSuccess/Failure task-token callbacks, and CDK's LogRetention provider setting retention on log groups Lambda and API Gateway create outside CloudFormation) do not support resource-level scoping. | AgentCorePolicy/Resource; AgentDispatch/DefaultPolicy/Resource; ApprovalConfig/DefaultPolicy/Resource (+25 more) |
| `AwsSolutions-IAM5[Resource::<Dependencies1F4ECB40.Arn>/*]` | Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function. | CreateGluePipelineRuntimeRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::<GeneratedCode2E3073B4.Arn>/*]` | Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function. | AgentCorePolicy/Resource; CreateCodeRuntimeRole/DefaultPolicy/Resource; SchemaMapping/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::<GeneratedCode2E3073B4.Arn>/shared/*]` | Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function. | SchemaCompare/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::<ProcessedFilesE1F7B8AA.Arn>/*]` | Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function. | AgentCorePolicy/Resource; FileValidation/DefaultPolicy/Resource; SchemaCompare/DefaultPolicy/Resource (+3 more) |
| `AwsSolutions-IAM5[Resource::<ProcessedFilesE1F7B8AA.Arn>/dq-reports/*]` | Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function. | DataQualityRuntimeRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::<SourceFilesBA99FC76.Arn>/*]` | Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function. | AgentCorePolicy/Resource; FileValidation/DefaultPolicy/Resource; NLPipelineBuilderRuntimeRole/DefaultPolicy/Resource (+4 more) |
| `AwsSolutions-IAM5[Resource::<TargetSchemas705BBA6F.Arn>/*]` | Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function. | AgentCorePolicy/Resource; Infrastructure/DefaultPolicy/Resource; SchemaCompare/DefaultPolicy/Resource (+1 more) |
| `AwsSolutions-IAM5[Resource::<Templates785E9C36.Arn>/*]` | Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function. | AgentCorePolicy/Resource |
| `AwsSolutions-IAM5[Resource::<TestOutputs59A774A4.Arn>/*]` | Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function. | AgentCorePolicy/Resource; TestCodeRuntimeRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::AgenticDataIngestion-Data-${environment}:ExportsOutputFnGetAttAuditLogTable8B03E3C8Arn985A1088/index/*]` | Wildcard under a specific cross-stack resource: GSIs of a named DynamoDB table or object keys under a named bucket. | Infrastructure/DefaultPolicy/Resource; SchemaMapping/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::AgenticDataIngestion-Data-${environment}:ExportsOutputFnGetAttWsConnectionsTable9E1AC3EBArnA4D51A9B/index/*]` | Wildcard under a specific cross-stack resource: GSIs of a named DynamoDB table or object keys under a named bucket. | Infrastructure/DefaultPolicy/Resource; Infrastructure/OverflowPolicy1/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:bedrock-agentcore:${region}:${account}:code-interpreter/*]` | AgentCore runtime/memory/code-interpreter resource ids are generated by the service; prefix-scoped where the service exposes stable prefixes. | CodeTesting/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:bedrock-agentcore:${region}:${account}:memory/*]` | AgentCore runtime/memory/code-interpreter resource ids are generated by the service; prefix-scoped where the service exposes stable prefixes. | ApprovalConfig/DefaultPolicy/Resource; ComplexMappingRuntimeRole/DefaultPolicy/Resource; Infrastructure/DefaultPolicy/Resource (+1 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:bedrock-agentcore:${region}:${account}:runtime/agentic_data_ingestion_agent_complex_mapping-*]` | AgentCore runtime/memory/code-interpreter resource ids are generated by the service; prefix-scoped where the service exposes stable prefixes. | SchemaMapping/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:bedrock-agentcore:${region}:${account}:runtime/agentic_data_ingestion_agent_cost_optimization-*]` | AgentCore runtime/memory/code-interpreter resource ids are generated by the service; prefix-scoped where the service exposes stable prefixes. | AgentDispatch/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:bedrock-agentcore:${region}:${account}:runtime/agentic_data_ingestion_agent_data_quality-*]` | AgentCore runtime/memory/code-interpreter resource ids are generated by the service; prefix-scoped where the service exposes stable prefixes. | AgentDispatch/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:bedrock-agentcore:${region}:${account}:runtime/agentic_data_ingestion_agent_self_healing-*]` | AgentCore runtime/memory/code-interpreter resource ids are generated by the service; prefix-scoped where the service exposes stable prefixes. | SelfHealingDispatch/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:cloudwatch:${region}:${account}:alarm:AgenticDataIngestion-Critical-*]` | Critical-alert alarms are created at runtime by the notification service's CloudWatch channel (one per alerting metric); alarm names are prefix-scoped to AgenticDataIngestion-Critical-. | ApprovalConfig/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion*/index/*]` | Project-prefix-scoped DynamoDB tables and their GSIs. | ApprovalConfig/DefaultPolicy/Resource; DynamoDBPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion*]` | Project-prefix-scoped DynamoDB tables and their GSIs. | ApprovalConfig/DefaultPolicy/Resource; DynamoDBPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_*]` | GlueJobRoleBoundary (permissions boundary, grants nothing): caps per-pipeline Glue roles at GetItem/PutItem on this deployment's tables (run state and JDBC watermarks). | GlueJobRoleBoundary/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_generated_code/index/*]` | GSIs of one named project table, for a statement that queries it; index names belong to the table's definition in the Data stack. | CreateCodeRuntimeRole/DefaultPolicy/Resource; CreateGluePipelineRuntimeRole/DefaultPolicy/Resource; TestCodeRuntimeRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_pipelines/index/*]` | GSIs of one named project table, for a statement that queries it; index names belong to the table's definition in the Data stack. | CreateCodeRuntimeRole/DefaultPolicy/Resource; CreateGluePipelineRuntimeRole/DefaultPolicy/Resource; CreateInfrastructureRuntimeRole/DefaultPolicy/Resource (+2 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_schema_mappings/index/*]` | GSIs of one named project table, for a statement that queries it; index names belong to the table's definition in the Data stack. | CreateCodeRuntimeRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_state/index/*]` | GSIs of one named project table, for a statement that queries it; index names belong to the table's definition in the Data stack. | CostOptimizationRuntimeRole/DefaultPolicy/Resource; CreateCodeRuntimeRole/DefaultPolicy/Resource; CreateGluePipelineRuntimeRole/DefaultPolicy/Resource (+3 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_test_results/index/*]` | GSIs of one named project table, for a statement that queries it; index names belong to the table's definition in the Data stack. | TestCodeRuntimeRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:ec2:${region}:${account}:network-interface/*]` | Glue JDBC connections attach ENIs with runtime-generated ids in this account/region. | GluePipeline/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:ec2:*:*:network-interface/*]` | GlueJobRoleBoundary (permissions boundary, grants nothing): Glue tags the VPC ENIs it creates for JDBC connections; the statement is conditioned on the aws-glue-service-resource tag key. | GlueJobRoleBoundary/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:events:${region}:${account}:event-bus/agentic-data-ingestion-*]` | Project-prefix-scoped EventBridge bus and per-pipeline schedule rules created at runtime. | ApprovalConfig/DefaultPolicy/Resource; CodeGeneration/DefaultPolicy/Resource; CodeTesting/DefaultPolicy/Resource (+11 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:events:${region}:${account}:rule/agentic-data-ingestion-*]` | Project-prefix-scoped EventBridge bus and per-pipeline schedule rules created at runtime. | Infrastructure/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:events:${region}:${account}:rule/agentic-data-ingestion-schedule-*]` | Project-prefix-scoped EventBridge bus and per-pipeline schedule rules created at runtime. | Infrastructure/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:execute-api:${region}:${account}:*/@connections/*]` | WebSocket connection ids are runtime values; scoped to the API's @connections management path. | Infrastructure/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:connection/*]` | glue_connection mode references pre-existing Glue Connections with arbitrary names; create_glue_pipeline reads the selected connection (and its tags) at job-creation time to resolve JDBC target credentials. Read-only (GetConnection/GetConnections/GetTags) and connection names are unknown at deploy time, so the read is account-wide; connection secrets remain protected by Secrets Manager. The dashboard API's wizard-time glue:CreateConnection/TagResource share this connection/* wildcard (the connection name does not yet exist at creation). The Glue discovery/runtime crawler execution role (GluePipelineRole) holds the same account-wide read for a separate reason: Glue validates a crawler's JdbcTargets[].ConnectionName by retrieving the connection as the PASSED role, not as the caller, so without it CreateCrawler fails with 'InvalidInputException: Unable to retrieve jdbc connection.' for every connection outside the agentic-data-ingestion-* prefix. Connection WRITE actions (Create/Update/DeleteConnection) stay prefix-scoped on both roles. | CreateGluePipelineRuntimeRole/DefaultPolicy/Resource; GlueJobRoleBoundary/Resource; GluePipeline/DefaultPolicy/Resource (+1 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:connection/agentic-data-ingestion-*]` | Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections). | CreateGluePipelineRuntimeRole/DefaultPolicy/Resource; GluePipeline/DefaultPolicy/Resource; Infrastructure/DefaultPolicy/Resource (+2 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:connection/agentic_data_ingestion*]` | Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections). | CreateGluePipelineRuntimeRole/DefaultPolicy/Resource; GluePipeline/DefaultPolicy/Resource; QualityChecks/DefaultPolicy/Resource (+1 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:crawler/agentic-data-ingestion-*]` | Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections). | AgentCorePolicy/Resource; CreateGluePipelineRuntimeRole/DefaultPolicy/Resource; GluePipeline/DefaultPolicy/Resource (+5 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:crawler/agentic_data_ingestion*]` | Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections). | AgentCorePolicy/Resource; CreateGluePipelineRuntimeRole/DefaultPolicy/Resource; GluePipeline/DefaultPolicy/Resource (+3 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:dataQualityRuleset/*]` | GlueJobRoleBoundary (permissions boundary, grants nothing): EvaluateDataQuality in a Glue ETL job publishes against a service-named ruleset resource; this is the documented minimum for a data quality ETL job. | GlueJobRoleBoundary/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:database/*]` | Read-only catalog browsing (GetDatabase/GetTable) for source discovery across the account catalog. | GlueJobRoleBoundary/Resource; Infrastructure/DefaultPolicy/Resource; SchemaMapping/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:database/agentic_data_ingestion*]` | Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections). | AgentCorePolicy/Resource; CreateGluePipelineRuntimeRole/DefaultPolicy/Resource; CreateInfrastructureRuntimeRole/DefaultPolicy/Resource (+6 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:database/agentic_data_ingestion_jdbc_discovery_*]` | Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections). | Infrastructure/DefaultPolicy/Resource; SchemaExtract/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:job/agentic-data-ingestion-*]` | Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections). | AgentCorePolicy/Resource; CostOptimizationRuntimeRole/DefaultPolicy/Resource; CreateGluePipelineRuntimeRole/DefaultPolicy/Resource (+8 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:job/agentic-data-ingestion-job-*]` | Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections). | Infrastructure/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:job/agentic_data_ingestion*]` | Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections). | AgentCorePolicy/Resource; CostOptimizationRuntimeRole/DefaultPolicy/Resource; CreateGluePipelineRuntimeRole/DefaultPolicy/Resource (+7 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:session/*]` | Glue interactive sessions verify AI-generated PySpark on real Spark; the session id is minted per verification run, so the ARN cannot be enumerated at deploy time. The session itself runs under the read-only AgenticDataIngestion-VerificationSession-Role, which holds no write action of any kind. | CodeTesting/DefaultPolicy/Resource; TestCodeRuntimeRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:table/*/*]` | Read-only catalog browsing (GetTable) for source discovery across the account catalog. | GlueJobRoleBoundary/Resource; Infrastructure/DefaultPolicy/Resource; SchemaMapping/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:table/agentic_data_ingestion*/*]` | Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections). | AgentCorePolicy/Resource; CreateGluePipelineRuntimeRole/DefaultPolicy/Resource; CreateInfrastructureRuntimeRole/DefaultPolicy/Resource (+6 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:table/agentic_data_ingestion_jdbc_discovery_*/*]` | Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections). | Infrastructure/DefaultPolicy/Resource; SchemaExtract/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:iam::${account}:role/AgenticDataIngestion-*-ExecutionRole]` | Per-pipeline Glue execution roles are created at runtime under the AgenticDataIngestion- prefix with an -ExecutionRole suffix. iam:CreateRole and iam:PutRolePolicy are condition-scoped to the GlueJobRoleBoundary permissions boundary, and iam:PassRole to glue.amazonaws.com (threat model T-03). | CreateGluePipelineRuntimeRole/DefaultPolicy/Resource; GluePipeline/DefaultPolicy/Resource; SelfHealingRuntimeRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:lambda:${region}:${account}:function:agentic_data_ingestion_*]` | Project-prefix-scoped Lambda invocation — agents invoke sibling pipeline functions of this deployment. | Infrastructure/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:logs:${region}:${account}:log-group:/aws-glue/*:*]` | Glue writes interactive-session logs to service-owned /aws-glue/* log groups whose names carry a runtime session id, so the group cannot be enumerated at deploy time. The verification session role holds CreateLogStream/PutLogEvents only — no CreateLogGroup and no read. | VerificationSessionRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:logs:${region}:${account}:log-group:/aws-glue/*]` | Glue writes interactive-session logs to service-owned /aws-glue/* log groups whose names carry a runtime session id, so the group cannot be enumerated at deploy time. The verification session role holds CreateLogStream/PutLogEvents only — no CreateLogGroup and no read. | GlueJobRoleBoundary/Resource; VerificationSessionRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:logs:${region}:${account}:log-group:/aws-glue/jobs/*:*]` | Glue writes job logs to the service-owned /aws-glue/jobs/* log groups whose stream names contain runtime job-run ids. | Infrastructure/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:logs:${region}:${account}:log-group:/aws-glue/jobs/*]` | Glue writes job logs to the service-owned /aws-glue/jobs/* log groups whose stream names contain runtime job-run ids. | Infrastructure/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:logs:${region}:${account}:log-group:/aws/bedrock-agentcore/*]` | AgentCore runtime and OTEL span log groups are created by the service per runtime/session. | ComplexMappingRuntimeRole/DefaultPolicy/Resource; CostOptimizationRuntimeRole/DefaultPolicy/Resource; CreateCodeRuntimeRole/DefaultPolicy/Resource (+7 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:logs:${region}:${account}:log-group:/aws/spans/*]` | AgentCore runtime and OTEL span log groups are created by the service per runtime/session. | ComplexMappingRuntimeRole/DefaultPolicy/Resource; CostOptimizationRuntimeRole/DefaultPolicy/Resource; CreateCodeRuntimeRole/DefaultPolicy/Resource (+7 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:s3:::*--table-s3/*]` | S3 Tables service-managed underlying buckets follow service naming with runtime-generated suffixes. | SchemaMapping/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:s3:::adi-*-${account}-${region}/*]` | Project buckets follow the adi-*-{account}-{region} convention; the wildcard spans only this deployment's nine project buckets. | CodeGeneration/DefaultPolicy/Resource; CodeTesting/DefaultPolicy/Resource; ComplexMappingRuntimeRole/DefaultPolicy/Resource (+7 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:s3:::adi-*-${account}-${region}]` | Project buckets follow the adi-*-{account}-{region} convention; the wildcard spans only this deployment's nine project buckets. | CodeGeneration/DefaultPolicy/Resource; CodeTesting/DefaultPolicy/Resource; ComplexMappingRuntimeRole/DefaultPolicy/Resource (+7 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:s3:::aws-athena-query-results-*/*]` | Athena's default results buckets are account-scoped with service-generated names. | AgentCorePolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:s3:::aws-athena-query-results-*]` | Athena's default results buckets are account-scoped with service-generated names. | AgentCorePolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:s3tables:${region}:${account}:bucket/*]` | S3 Tables table buckets and tables are created per pipeline at runtime; ids cannot be enumerated at deploy time. | AgentCorePolicy/Resource; CreateGluePipelineRuntimeRole/DefaultPolicy/Resource; CreateInfrastructureRuntimeRole/DefaultPolicy/Resource (+6 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:s3tables:${region}:${account}:table/*]` | S3 Tables table buckets and tables are created per pipeline at runtime; ids cannot be enumerated at deploy time. | AgentCorePolicy/Resource; CreateInfrastructureRuntimeRole/DefaultPolicy/Resource; GluePipeline/DefaultPolicy/Resource (+3 more) |
| `AwsSolutions-IAM5[Resource::arn:aws:secretsmanager:${region}:${account}:secret:*]` | GlueJobRoleBoundary (permissions boundary, grants nothing): a JDBC credential is whatever secret its Glue connection's SECRET_ID names, which the operator chooses; each per-pipeline role's inline policy grants only that one secret ARN. | GlueJobRoleBoundary/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:secretsmanager:${region}:${account}:secret:agentglue/test-fixtures/*]` | Legacy P18 JDBC test-fixture credentials remain under the pre-rename agentglue/test-fixtures/ path; the Glue discovery/runtime crawler role must read them to run JDBC crawls. Secrets Manager ARNs additionally carry random six-char suffixes. Remove once these fixtures are renamed to the agentic-data-ingestion/ prefix. | GluePipeline/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:secretsmanager:${region}:${account}:secret:agentic-data-ingestion/*]` | Per-pipeline JDBC credentials are stored under the project's secret path at runtime; Secrets Manager ARNs additionally carry random six-char suffixes. | GluePipeline/DefaultPolicy/Resource; Infrastructure/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:sqs:${region}:${account}:agentic-data-ingestion-pipeline-triggers-*]` | Project-prefix-scoped SQS queues. | Infrastructure/DefaultPolicy/Resource; PipelineTrigger/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:states:${region}:${account}:execution:agentic-data-ingestion-*:*]` | Project-prefix-scoped Step Functions state machine and its runtime-named executions. | Infrastructure/DefaultPolicy/Resource; JobCompletion/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:states:${region}:${account}:stateMachine:agentic-data-ingestion-*]` | Project-prefix-scoped Step Functions state machine and its runtime-named executions. | EventBridgeSfnRole/DefaultPolicy/Resource; Infrastructure/DefaultPolicy/Resource; PipelineTrigger/DefaultPolicy/Resource (+1 more) |

### agentcore

| Finding ID | Justification | Resources |
|------------|---------------|-----------|
| `AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]` | AWS managed AWSLambdaBasicExecutionRole provides baseline CloudWatch logging for Lambda functions; acceptable for a sample — replace with customer-managed equivalents in stricter environments. | TxSearchSetupRole/Resource |
| `AwsSolutions-IAM5[Resource::*]` | The actions in this statement (X-Ray tracing, CloudWatch PutMetricData, Bedrock model invocation, ECR auth token, Step Functions SendTaskSuccess/Failure task-token callbacks, and CDK's LogRetention provider setting retention on log groups Lambda and API Gateway create outside CloudFormation) do not support resource-level scoping. | TxSearchSetupRole/DefaultPolicy/Resource |

### compute

| Finding ID | Justification | Resources |
|------------|---------------|-----------|
| `AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]` | AWS managed AWSLambdaBasicExecutionRole provides baseline CloudWatch logging for Lambda functions; acceptable for a sample — replace with customer-managed equivalents in stricter environments. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/Resource; LogRetentionaae0aa3c5b4d4f87b02d85b201efdd8a/ServiceRole/Resource; WsAuthorizerRole/Resource |
| `AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs]` | AWS managed AmazonAPIGatewayPushToCloudWatchLogs is the standard account-level role API Gateway requires to write execution/access logs. | DashboardRestApi/CloudWatchRole/Resource |
| `AwsSolutions-IAM5[Action::s3:Abort*]` | CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Action::s3:DeleteObject*]` | CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Action::s3:GetBucket*]` | CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Action::s3:GetObject*]` | CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Action::s3:List*]` | CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::*]` | The actions in this statement (X-Ray tracing, CloudWatch PutMetricData, Bedrock model invocation, ECR auth token, Step Functions SendTaskSuccess/Failure task-token callbacks, and CDK's LogRetention provider setting retention on log groups Lambda and API Gateway create outside CloudFormation) do not support resource-level scoping. | LogRetentionaae0aa3c5b4d4f87b02d85b201efdd8a/ServiceRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::AgenticDataIngestion-Foundation-${environment}:ExportsOutputFnGetAttDependencies1F4ECB40Arn2EC223A6/*]` | Wildcard under a specific cross-stack resource: GSIs of a named DynamoDB table or object keys under a named bucket. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::AgenticDataIngestion-Foundation-${environment}:ExportsOutputFnGetAttTemplates785E9C36Arn3C3F8920/*]` | Wildcard under a specific cross-stack resource: GSIs of a named DynamoDB table or object keys under a named bucket. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:<AWS::Partition>:s3:::cdk-hnb659fds-assets-${account}-${region}/*]` | BucketDeployment reads from the CDK bootstrap assets bucket; keys are content-hashed at deploy time. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource |

### orchestration

| Finding ID | Justification | Resources |
|------------|---------------|-----------|
| `AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]` | AWS managed AWSLambdaBasicExecutionRole provides baseline CloudWatch logging for Lambda functions; acceptable for a sample — replace with customer-managed equivalents in stricter environments. | ApprovalStatusUpdateRole/Resource; FailureRecorderRole/Resource; GlueJobStateUpdateRole/Resource (+4 more) |
| `AwsSolutions-IAM5[Resource::*]` | The actions in this statement (X-Ray tracing, CloudWatch PutMetricData, Bedrock model invocation, ECR auth token, Step Functions SendTaskSuccess/Failure task-token callbacks, and CDK's LogRetention provider setting retention on log groups Lambda and API Gateway create outside CloudFormation) do not support resource-level scoping. | FailureRecorderRole/DefaultPolicy/Resource; GlueJobStateUpdateRole/DefaultPolicy/Resource; LogRetentionaae0aa3c5b4d4f87b02d85b201efdd8a/ServiceRole/DefaultPolicy/Resource (+2 more) |
| `AwsSolutions-IAM5[Resource::<FailureRecorderFunctionCD8B825B.Arn>:*]` | Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function. | StepFunctionsExecutionRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::<LockReleaseFunction49062137.Arn>:*]` | Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function. | StepFunctionsExecutionRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::AgenticDataIngestion-Compute-${environment}:ExportsOutputFnGetAttCreateQualityChecks9FBD0F59Arn47BF12D7:*]` | grantInvoke emits <functionArn>:* to cover qualified ARNs (versions/aliases) of the specific cross-stack function. | StepFunctionsExecutionRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::AgenticDataIngestion-Compute-${environment}:ExportsOutputFnGetAttFileValidationCB8E028FArnDE384992:*]` | grantInvoke emits <functionArn>:* to cover qualified ARNs (versions/aliases) of the specific cross-stack function. | StepFunctionsExecutionRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::AgenticDataIngestion-Compute-${environment}:ExportsOutputFnGetAttSchemaComparisonB89D3E8BArn04318E7D:*]` | grantInvoke emits <functionArn>:* to cover qualified ARNs (versions/aliases) of the specific cross-stack function. | StepFunctionsExecutionRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::AgenticDataIngestion-Compute-${environment}:ExportsOutputFnGetAttSchemaExtraction3801DA98Arn9CD2F96C:*]` | grantInvoke emits <functionArn>:* to cover qualified ARNs (versions/aliases) of the specific cross-stack function. | StepFunctionsExecutionRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::AgenticDataIngestion-Compute-${environment}:ExportsOutputFnGetAttSchemaMapping5EA572D2ArnFCC7322C:*]` | grantInvoke emits <functionArn>:* to cover qualified ARNs (versions/aliases) of the specific cross-stack function. | StepFunctionsExecutionRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:job/agentic-data-ingestion-*]` | Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections). | GlueJobStateUpdateRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:job/agentic_data_ingestion*]` | Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections). | GlueJobStateUpdateRole/DefaultPolicy/Resource |

### hosting

| Finding ID | Justification | Resources |
|------------|---------------|-----------|
| `AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]` | AWS managed AWSLambdaBasicExecutionRole provides baseline CloudWatch logging for Lambda functions; acceptable for a sample — replace with customer-managed equivalents in stricter environments. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/Resource; LogRetentionaae0aa3c5b4d4f87b02d85b201efdd8a/ServiceRole/Resource |
| `AwsSolutions-IAM5[Action::s3:Abort*]` | CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Action::s3:DeleteObject*]` | CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Action::s3:GetBucket*]` | CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Action::s3:GetObject*]` | CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Action::s3:List*]` | CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::*]` | The actions in this statement (X-Ray tracing, CloudWatch PutMetricData, Bedrock model invocation, ECR auth token, Step Functions SendTaskSuccess/Failure task-token callbacks, and CDK's LogRetention provider setting retention on log groups Lambda and API Gateway create outside CloudFormation) do not support resource-level scoping. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource; LogRetentionaae0aa3c5b4d4f87b02d85b201efdd8a/ServiceRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::<SiteBucket397A1860.Arn>/*]` | Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource |
| `AwsSolutions-IAM5[Resource::arn:<AWS::Partition>:s3:::cdk-hnb659fds-assets-${account}-${region}/*]` | BucketDeployment reads from the CDK bootstrap assets bucket; keys are content-hashed at deploy time. | Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource |

