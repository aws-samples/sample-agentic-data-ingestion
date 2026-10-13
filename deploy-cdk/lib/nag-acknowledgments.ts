// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// GENERATED FILE — do not edit by hand.
// Regenerate with: python3 scripts/generate_nag_acknowledgments.py
// (procedure in docs/CDK-NAG.md). Every entry is a granular cdk-nag
// finding ID with a written justification (publication-cleanup R4.3/R4.5).
//
// WHY raw metadata: Validations.acknowledge() rejects granular IDs (their
// '[Resource::...]' payload contains '::', which qualifyId reserves), but
// cdk-nag's isAcknowledged() matches finding IDs against the raw keys of
// the aws:cdk:acknowledged-rules metadata — so we write that key directly.
import * as cdk from 'aws-cdk-lib';

export interface NagAcknowledgmentStacks {
  readonly foundation?: cdk.Stack;
  readonly data?: cdk.Stack;
  readonly auth?: cdk.Stack;
  readonly agentcore?: cdk.Stack;
  readonly compute?: cdk.Stack;
  readonly orchestration?: cdk.Stack;
  readonly edgeWaf?: cdk.Stack;
  readonly hosting?: cdk.Stack;
}

/** Apply granular cdk-nag acknowledgments to each stack (no-op for absent stacks). */
export function applyNagAcknowledgments(stacks: NagAcknowledgmentStacks, environment: string): void {
  void environment; // used inside generated template literals
  if (stacks.foundation) {
    const account = stacks.foundation.account;
    const region = stacks.foundation.region;
    void account; void region;
    stacks.foundation.node.addMetadata(cdk.Validations.ACKNOWLEDGED_RULES_METADATA_KEY, {
      [`AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]`]:
        "AWS managed AWSLambdaBasicExecutionRole provides baseline CloudWatch logging for Lambda functions; acceptable for a sample \u2014 replace with customer-managed equivalents in stricter environments.",
      [`AwsSolutions-IAM5[Action::athena:*]`]:
        "Analytics MCP server read/query surface; Athena results land in scoped buckets and EMR access is read-only.",
      [`AwsSolutions-IAM5[Action::glue:*]`]:
        "Glue lifecycle admin for runtime-created pipeline resources; the same statement prefix-scopes resources to agentic_data_ingestion*/agentic-data-ingestion-*.",
      [`AwsSolutions-IAM5[Action::glue:BatchGet*]`]:
        "Read-only Glue discovery; the same statement prefix-scopes resources to project naming forms.",
      [`AwsSolutions-IAM5[Action::glue:Get*]`]:
        "Read-only Glue discovery; the same statement prefix-scopes resources to project naming forms.",
      [`AwsSolutions-IAM5[Action::glue:List*]`]:
        "Read-only Glue discovery; the same statement prefix-scopes resources to project naming forms.",
      [`AwsSolutions-IAM5[Action::s3:GetBucket*]`]:
        "CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets.",
      [`AwsSolutions-IAM5[Action::s3:GetObject*]`]:
        "CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets.",
      [`AwsSolutions-IAM5[Action::s3:List*]`]:
        "CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets.",
      [`AwsSolutions-IAM5[Action::s3tables:*]`]:
        "S3 Tables (Iceberg) tables are created per pipeline at runtime; the API is namespaced per table bucket so actions cannot be enumerated at deploy time.",
      [`AwsSolutions-IAM5[Action::s3tables:Get*]`]:
        "Read-only S3 Tables discovery for the MCP server runtimes, which run with write access disabled; resources are the account's table buckets.",
      [`AwsSolutions-IAM5[Action::s3tables:List*]`]:
        "Read-only S3 Tables discovery for the MCP server runtimes, which run with write access disabled; resources are the account's table buckets.",
      [`AwsSolutions-IAM5[Resource::*]`]:
        "The actions in this statement (X-Ray tracing, CloudWatch PutMetricData, Bedrock model invocation, ECR auth token, Step Functions SendTaskSuccess/Failure task-token callbacks, and CDK's LogRetention provider setting retention on log groups Lambda and API Gateway create outside CloudFormation) do not support resource-level scoping.",
      [`AwsSolutions-IAM5[Resource::<Dependencies1F4ECB40.Arn>/*]`]:
        "Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function.",
      [`AwsSolutions-IAM5[Resource::<GeneratedCode2E3073B4.Arn>/*]`]:
        "Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function.",
      [`AwsSolutions-IAM5[Resource::<GeneratedCode2E3073B4.Arn>/shared/*]`]:
        "Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function.",
      [`AwsSolutions-IAM5[Resource::<ProcessedFilesE1F7B8AA.Arn>/*]`]:
        "Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function.",
      [`AwsSolutions-IAM5[Resource::<ProcessedFilesE1F7B8AA.Arn>/dq-reports/*]`]:
        "Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function.",
      [`AwsSolutions-IAM5[Resource::<SourceFilesBA99FC76.Arn>/*]`]:
        "Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function.",
      [`AwsSolutions-IAM5[Resource::<TargetSchemas705BBA6F.Arn>/*]`]:
        "Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function.",
      [`AwsSolutions-IAM5[Resource::<Templates785E9C36.Arn>/*]`]:
        "Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function.",
      [`AwsSolutions-IAM5[Resource::<TestOutputs59A774A4.Arn>/*]`]:
        "Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function.",
      [`AwsSolutions-IAM5[Resource::AgenticDataIngestion-Data-${environment}:ExportsOutputFnGetAttAuditLogTable8B03E3C8Arn985A1088/index/*]`]:
        "Wildcard under a specific cross-stack resource: GSIs of a named DynamoDB table or object keys under a named bucket.",
      [`AwsSolutions-IAM5[Resource::AgenticDataIngestion-Data-${environment}:ExportsOutputFnGetAttWsConnectionsTable9E1AC3EBArnA4D51A9B/index/*]`]:
        "Wildcard under a specific cross-stack resource: GSIs of a named DynamoDB table or object keys under a named bucket.",
      [`AwsSolutions-IAM5[Resource::arn:aws:bedrock-agentcore:${region}:${account}:code-interpreter/*]`]:
        "AgentCore runtime/memory/code-interpreter resource ids are generated by the service; prefix-scoped where the service exposes stable prefixes.",
      [`AwsSolutions-IAM5[Resource::arn:aws:bedrock-agentcore:${region}:${account}:memory/*]`]:
        "AgentCore runtime/memory/code-interpreter resource ids are generated by the service; prefix-scoped where the service exposes stable prefixes.",
      [`AwsSolutions-IAM5[Resource::arn:aws:bedrock-agentcore:${region}:${account}:runtime/agentic_data_ingestion_agent_complex_mapping-*]`]:
        "AgentCore runtime/memory/code-interpreter resource ids are generated by the service; prefix-scoped where the service exposes stable prefixes.",
      [`AwsSolutions-IAM5[Resource::arn:aws:bedrock-agentcore:${region}:${account}:runtime/agentic_data_ingestion_agent_cost_optimization-*]`]:
        "AgentCore runtime/memory/code-interpreter resource ids are generated by the service; prefix-scoped where the service exposes stable prefixes.",
      [`AwsSolutions-IAM5[Resource::arn:aws:bedrock-agentcore:${region}:${account}:runtime/agentic_data_ingestion_agent_data_quality-*]`]:
        "AgentCore runtime/memory/code-interpreter resource ids are generated by the service; prefix-scoped where the service exposes stable prefixes.",
      [`AwsSolutions-IAM5[Resource::arn:aws:bedrock-agentcore:${region}:${account}:runtime/agentic_data_ingestion_agent_self_healing-*]`]:
        "AgentCore runtime/memory/code-interpreter resource ids are generated by the service; prefix-scoped where the service exposes stable prefixes.",
      [`AwsSolutions-IAM5[Resource::arn:aws:cloudwatch:${region}:${account}:alarm:AgenticDataIngestion-Critical-*]`]:
        "Critical-alert alarms are created at runtime by the notification service's CloudWatch channel (one per alerting metric); alarm names are prefix-scoped to AgenticDataIngestion-Critical-.",
      [`AwsSolutions-IAM5[Resource::arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion*/index/*]`]:
        "Project-prefix-scoped DynamoDB tables and their GSIs.",
      [`AwsSolutions-IAM5[Resource::arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion*]`]:
        "Project-prefix-scoped DynamoDB tables and their GSIs.",
      [`AwsSolutions-IAM5[Resource::arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_*]`]:
        "GlueJobRoleBoundary (permissions boundary, grants nothing): caps per-pipeline Glue roles at GetItem/PutItem on this deployment's tables (run state and JDBC watermarks).",
      [`AwsSolutions-IAM5[Resource::arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_generated_code/index/*]`]:
        "GSIs of one named project table, for a statement that queries it; index names belong to the table's definition in the Data stack.",
      [`AwsSolutions-IAM5[Resource::arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_pipelines/index/*]`]:
        "GSIs of one named project table, for a statement that queries it; index names belong to the table's definition in the Data stack.",
      [`AwsSolutions-IAM5[Resource::arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_schema_mappings/index/*]`]:
        "GSIs of one named project table, for a statement that queries it; index names belong to the table's definition in the Data stack.",
      [`AwsSolutions-IAM5[Resource::arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_state/index/*]`]:
        "GSIs of one named project table, for a statement that queries it; index names belong to the table's definition in the Data stack.",
      [`AwsSolutions-IAM5[Resource::arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_test_results/index/*]`]:
        "GSIs of one named project table, for a statement that queries it; index names belong to the table's definition in the Data stack.",
      [`AwsSolutions-IAM5[Resource::arn:aws:ec2:${region}:${account}:network-interface/*]`]:
        "Glue JDBC connections attach ENIs with runtime-generated ids in this account/region.",
      [`AwsSolutions-IAM5[Resource::arn:aws:ec2:*:*:network-interface/*]`]:
        "GlueJobRoleBoundary (permissions boundary, grants nothing): Glue tags the VPC ENIs it creates for JDBC connections; the statement is conditioned on the aws-glue-service-resource tag key.",
      [`AwsSolutions-IAM5[Resource::arn:aws:events:${region}:${account}:event-bus/agentic-data-ingestion-*]`]:
        "Project-prefix-scoped EventBridge bus and per-pipeline schedule rules created at runtime.",
      [`AwsSolutions-IAM5[Resource::arn:aws:events:${region}:${account}:rule/agentic-data-ingestion-*]`]:
        "Project-prefix-scoped EventBridge bus and per-pipeline schedule rules created at runtime.",
      [`AwsSolutions-IAM5[Resource::arn:aws:events:${region}:${account}:rule/agentic-data-ingestion-schedule-*]`]:
        "Project-prefix-scoped EventBridge bus and per-pipeline schedule rules created at runtime.",
      [`AwsSolutions-IAM5[Resource::arn:aws:execute-api:${region}:${account}:*/@connections/*]`]:
        "WebSocket connection ids are runtime values; scoped to the API's @connections management path.",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:connection/*]`]:
        "glue_connection mode references pre-existing Glue Connections with arbitrary names; create_glue_pipeline reads the selected connection (and its tags) at job-creation time to resolve JDBC target credentials. Read-only (GetConnection/GetConnections/GetTags) and connection names are unknown at deploy time, so the read is account-wide; connection secrets remain protected by Secrets Manager. The dashboard API's wizard-time glue:CreateConnection/TagResource share this connection/* wildcard (the connection name does not yet exist at creation). The Glue discovery/runtime crawler execution role (GluePipelineRole) holds the same account-wide read for a separate reason: Glue validates a crawler's JdbcTargets[].ConnectionName by retrieving the connection as the PASSED role, not as the caller, so without it CreateCrawler fails with 'InvalidInputException: Unable to retrieve jdbc connection.' for every connection outside the agentic-data-ingestion-* prefix. Connection WRITE actions (Create/Update/DeleteConnection) stay prefix-scoped on both roles.",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:connection/agentic-data-ingestion-*]`]:
        "Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections).",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:connection/agentic_data_ingestion*]`]:
        "Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections).",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:crawler/agentic-data-ingestion-*]`]:
        "Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections).",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:crawler/agentic_data_ingestion*]`]:
        "Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections).",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:dataQualityRuleset/*]`]:
        "GlueJobRoleBoundary (permissions boundary, grants nothing): EvaluateDataQuality in a Glue ETL job publishes against a service-named ruleset resource; this is the documented minimum for a data quality ETL job.",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:database/*]`]:
        "Read-only catalog browsing (GetDatabase/GetTable) for source discovery across the account catalog.",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:database/agentic_data_ingestion*]`]:
        "Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections).",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:database/agentic_data_ingestion_jdbc_discovery_*]`]:
        "Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections).",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:job/agentic-data-ingestion-*]`]:
        "Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections).",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:job/agentic-data-ingestion-job-*]`]:
        "Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections).",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:job/agentic_data_ingestion*]`]:
        "Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections).",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:session/*]`]:
        "Glue interactive sessions verify AI-generated PySpark on real Spark; the session id is minted per verification run, so the ARN cannot be enumerated at deploy time. The session itself runs under the read-only AgenticDataIngestion-VerificationSession-Role, which holds no write action of any kind.",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:table/*/*]`]:
        "Read-only catalog browsing (GetTable) for source discovery across the account catalog.",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:table/agentic_data_ingestion*/*]`]:
        "Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections).",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:table/agentic_data_ingestion_jdbc_discovery_*/*]`]:
        "Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections).",
      [`AwsSolutions-IAM5[Resource::arn:aws:iam::${account}:role/AgenticDataIngestion-*-ExecutionRole]`]:
        "Per-pipeline Glue execution roles are created at runtime under the AgenticDataIngestion- prefix with an -ExecutionRole suffix. iam:CreateRole and iam:PutRolePolicy are condition-scoped to the GlueJobRoleBoundary permissions boundary, and iam:PassRole to glue.amazonaws.com (threat model T-03).",
      [`AwsSolutions-IAM5[Resource::arn:aws:lambda:${region}:${account}:function:agentic_data_ingestion_*]`]:
        "Project-prefix-scoped Lambda invocation \u2014 agents invoke sibling pipeline functions of this deployment.",
      [`AwsSolutions-IAM5[Resource::arn:aws:logs:${region}:${account}:log-group:/aws-glue/*:*]`]:
        "Glue writes interactive-session logs to service-owned /aws-glue/* log groups whose names carry a runtime session id, so the group cannot be enumerated at deploy time. The verification session role holds CreateLogStream/PutLogEvents only \u2014 no CreateLogGroup and no read.",
      [`AwsSolutions-IAM5[Resource::arn:aws:logs:${region}:${account}:log-group:/aws-glue/*]`]:
        "Glue writes interactive-session logs to service-owned /aws-glue/* log groups whose names carry a runtime session id, so the group cannot be enumerated at deploy time. The verification session role holds CreateLogStream/PutLogEvents only \u2014 no CreateLogGroup and no read.",
      [`AwsSolutions-IAM5[Resource::arn:aws:logs:${region}:${account}:log-group:/aws-glue/jobs/*:*]`]:
        "Glue writes job logs to the service-owned /aws-glue/jobs/* log groups whose stream names contain runtime job-run ids.",
      [`AwsSolutions-IAM5[Resource::arn:aws:logs:${region}:${account}:log-group:/aws-glue/jobs/*]`]:
        "Glue writes job logs to the service-owned /aws-glue/jobs/* log groups whose stream names contain runtime job-run ids.",
      [`AwsSolutions-IAM5[Resource::arn:aws:logs:${region}:${account}:log-group:/aws/bedrock-agentcore/*]`]:
        "AgentCore runtime and OTEL span log groups are created by the service per runtime/session.",
      [`AwsSolutions-IAM5[Resource::arn:aws:logs:${region}:${account}:log-group:/aws/spans/*]`]:
        "AgentCore runtime and OTEL span log groups are created by the service per runtime/session.",
      [`AwsSolutions-IAM5[Resource::arn:aws:s3:::*--table-s3/*]`]:
        "S3 Tables service-managed underlying buckets follow service naming with runtime-generated suffixes.",
      [`AwsSolutions-IAM5[Resource::arn:aws:s3:::adi-*-${account}-${region}/*]`]:
        "Project buckets follow the adi-*-{account}-{region} convention; the wildcard spans only this deployment's nine project buckets.",
      [`AwsSolutions-IAM5[Resource::arn:aws:s3:::adi-*-${account}-${region}]`]:
        "Project buckets follow the adi-*-{account}-{region} convention; the wildcard spans only this deployment's nine project buckets.",
      [`AwsSolutions-IAM5[Resource::arn:aws:s3:::aws-athena-query-results-*/*]`]:
        "Athena's default results buckets are account-scoped with service-generated names.",
      [`AwsSolutions-IAM5[Resource::arn:aws:s3:::aws-athena-query-results-*]`]:
        "Athena's default results buckets are account-scoped with service-generated names.",
      [`AwsSolutions-IAM5[Resource::arn:aws:s3tables:${region}:${account}:bucket/*]`]:
        "S3 Tables table buckets and tables are created per pipeline at runtime; ids cannot be enumerated at deploy time.",
      [`AwsSolutions-IAM5[Resource::arn:aws:s3tables:${region}:${account}:table/*]`]:
        "S3 Tables table buckets and tables are created per pipeline at runtime; ids cannot be enumerated at deploy time.",
      [`AwsSolutions-IAM5[Resource::arn:aws:secretsmanager:${region}:${account}:secret:*]`]:
        "GlueJobRoleBoundary (permissions boundary, grants nothing): a JDBC credential is whatever secret its Glue connection's SECRET_ID names, which the operator chooses; each per-pipeline role's inline policy grants only that one secret ARN.",
      [`AwsSolutions-IAM5[Resource::arn:aws:secretsmanager:${region}:${account}:secret:agentglue/test-fixtures/*]`]:
        "Legacy P18 JDBC test-fixture credentials remain under the pre-rename agentglue/test-fixtures/ path; the Glue discovery/runtime crawler role must read them to run JDBC crawls. Secrets Manager ARNs additionally carry random six-char suffixes. Remove once these fixtures are renamed to the agentic-data-ingestion/ prefix.",
      [`AwsSolutions-IAM5[Resource::arn:aws:secretsmanager:${region}:${account}:secret:agentic-data-ingestion/*]`]:
        "Per-pipeline JDBC credentials are stored under the project's secret path at runtime; Secrets Manager ARNs additionally carry random six-char suffixes.",
      [`AwsSolutions-IAM5[Resource::arn:aws:sqs:${region}:${account}:agentic-data-ingestion-pipeline-triggers-*]`]:
        "Project-prefix-scoped SQS queues.",
      [`AwsSolutions-IAM5[Resource::arn:aws:states:${region}:${account}:execution:agentic-data-ingestion-*:*]`]:
        "Project-prefix-scoped Step Functions state machine and its runtime-named executions.",
      [`AwsSolutions-IAM5[Resource::arn:aws:states:${region}:${account}:stateMachine:agentic-data-ingestion-*]`]:
        "Project-prefix-scoped Step Functions state machine and its runtime-named executions.",
    });
  }
  if (stacks.agentcore) {
    const account = stacks.agentcore.account;
    const region = stacks.agentcore.region;
    void account; void region;
    stacks.agentcore.node.addMetadata(cdk.Validations.ACKNOWLEDGED_RULES_METADATA_KEY, {
      [`AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]`]:
        "AWS managed AWSLambdaBasicExecutionRole provides baseline CloudWatch logging for Lambda functions; acceptable for a sample \u2014 replace with customer-managed equivalents in stricter environments.",
      [`AwsSolutions-IAM5[Resource::*]`]:
        "The actions in this statement (X-Ray tracing, CloudWatch PutMetricData, Bedrock model invocation, ECR auth token, Step Functions SendTaskSuccess/Failure task-token callbacks, and CDK's LogRetention provider setting retention on log groups Lambda and API Gateway create outside CloudFormation) do not support resource-level scoping.",
    });
  }
  if (stacks.compute) {
    const account = stacks.compute.account;
    const region = stacks.compute.region;
    void account; void region;
    stacks.compute.node.addMetadata(cdk.Validations.ACKNOWLEDGED_RULES_METADATA_KEY, {
      [`AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]`]:
        "AWS managed AWSLambdaBasicExecutionRole provides baseline CloudWatch logging for Lambda functions; acceptable for a sample \u2014 replace with customer-managed equivalents in stricter environments.",
      [`AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs]`]:
        "AWS managed AmazonAPIGatewayPushToCloudWatchLogs is the standard account-level role API Gateway requires to write execution/access logs.",
      [`AwsSolutions-IAM5[Action::s3:Abort*]`]:
        "CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets.",
      [`AwsSolutions-IAM5[Action::s3:DeleteObject*]`]:
        "CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets.",
      [`AwsSolutions-IAM5[Action::s3:GetBucket*]`]:
        "CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets.",
      [`AwsSolutions-IAM5[Action::s3:GetObject*]`]:
        "CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets.",
      [`AwsSolutions-IAM5[Action::s3:List*]`]:
        "CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets.",
      [`AwsSolutions-IAM5[Resource::*]`]:
        "The actions in this statement (X-Ray tracing, CloudWatch PutMetricData, Bedrock model invocation, ECR auth token, Step Functions SendTaskSuccess/Failure task-token callbacks, and CDK's LogRetention provider setting retention on log groups Lambda and API Gateway create outside CloudFormation) do not support resource-level scoping.",
      [`AwsSolutions-IAM5[Resource::AgenticDataIngestion-Foundation-${environment}:ExportsOutputFnGetAttDependencies1F4ECB40Arn2EC223A6/*]`]:
        "Wildcard under a specific cross-stack resource: GSIs of a named DynamoDB table or object keys under a named bucket.",
      [`AwsSolutions-IAM5[Resource::AgenticDataIngestion-Foundation-${environment}:ExportsOutputFnGetAttTemplates785E9C36Arn3C3F8920/*]`]:
        "Wildcard under a specific cross-stack resource: GSIs of a named DynamoDB table or object keys under a named bucket.",
      [`AwsSolutions-IAM5[Resource::arn:<AWS::Partition>:s3:::cdk-hnb659fds-assets-${account}-${region}/*]`]:
        "BucketDeployment reads from the CDK bootstrap assets bucket; keys are content-hashed at deploy time.",
    });
  }
  if (stacks.orchestration) {
    const account = stacks.orchestration.account;
    const region = stacks.orchestration.region;
    void account; void region;
    stacks.orchestration.node.addMetadata(cdk.Validations.ACKNOWLEDGED_RULES_METADATA_KEY, {
      [`AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]`]:
        "AWS managed AWSLambdaBasicExecutionRole provides baseline CloudWatch logging for Lambda functions; acceptable for a sample \u2014 replace with customer-managed equivalents in stricter environments.",
      [`AwsSolutions-IAM5[Resource::*]`]:
        "The actions in this statement (X-Ray tracing, CloudWatch PutMetricData, Bedrock model invocation, ECR auth token, Step Functions SendTaskSuccess/Failure task-token callbacks, and CDK's LogRetention provider setting retention on log groups Lambda and API Gateway create outside CloudFormation) do not support resource-level scoping.",
      [`AwsSolutions-IAM5[Resource::<FailureRecorderFunctionCD8B825B.Arn>:*]`]:
        "Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function.",
      [`AwsSolutions-IAM5[Resource::<LockReleaseFunction49062137.Arn>:*]`]:
        "Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function.",
      [`AwsSolutions-IAM5[Resource::AgenticDataIngestion-Compute-${environment}:ExportsOutputFnGetAttCreateQualityChecks9FBD0F59Arn47BF12D7:*]`]:
        "grantInvoke emits <functionArn>:* to cover qualified ARNs (versions/aliases) of the specific cross-stack function.",
      [`AwsSolutions-IAM5[Resource::AgenticDataIngestion-Compute-${environment}:ExportsOutputFnGetAttFileValidationCB8E028FArnDE384992:*]`]:
        "grantInvoke emits <functionArn>:* to cover qualified ARNs (versions/aliases) of the specific cross-stack function.",
      [`AwsSolutions-IAM5[Resource::AgenticDataIngestion-Compute-${environment}:ExportsOutputFnGetAttSchemaComparisonB89D3E8BArn04318E7D:*]`]:
        "grantInvoke emits <functionArn>:* to cover qualified ARNs (versions/aliases) of the specific cross-stack function.",
      [`AwsSolutions-IAM5[Resource::AgenticDataIngestion-Compute-${environment}:ExportsOutputFnGetAttSchemaExtraction3801DA98Arn9CD2F96C:*]`]:
        "grantInvoke emits <functionArn>:* to cover qualified ARNs (versions/aliases) of the specific cross-stack function.",
      [`AwsSolutions-IAM5[Resource::AgenticDataIngestion-Compute-${environment}:ExportsOutputFnGetAttSchemaMapping5EA572D2ArnFCC7322C:*]`]:
        "grantInvoke emits <functionArn>:* to cover qualified ARNs (versions/aliases) of the specific cross-stack function.",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:job/agentic-data-ingestion-*]`]:
        "Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections).",
      [`AwsSolutions-IAM5[Resource::arn:aws:glue:${region}:${account}:job/agentic_data_ingestion*]`]:
        "Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot be enumerated at deploy time, so they are prefix-scoped to the project naming forms (snake for databases/tables, kebab for jobs/crawlers/connections).",
    });
  }
  if (stacks.hosting) {
    const account = stacks.hosting.account;
    const region = stacks.hosting.region;
    void account; void region;
    stacks.hosting.node.addMetadata(cdk.Validations.ACKNOWLEDGED_RULES_METADATA_KEY, {
      [`AwsSolutions-IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]`]:
        "AWS managed AWSLambdaBasicExecutionRole provides baseline CloudWatch logging for Lambda functions; acceptable for a sample \u2014 replace with customer-managed equivalents in stricter environments.",
      [`AwsSolutions-IAM5[Action::s3:Abort*]`]:
        "CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets.",
      [`AwsSolutions-IAM5[Action::s3:DeleteObject*]`]:
        "CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets.",
      [`AwsSolutions-IAM5[Action::s3:GetBucket*]`]:
        "CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets.",
      [`AwsSolutions-IAM5[Action::s3:GetObject*]`]:
        "CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets.",
      [`AwsSolutions-IAM5[Action::s3:List*]`]:
        "CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is resource-scoped to specific project buckets.",
      [`AwsSolutions-IAM5[Resource::*]`]:
        "The actions in this statement (X-Ray tracing, CloudWatch PutMetricData, Bedrock model invocation, ECR auth token, Step Functions SendTaskSuccess/Failure task-token callbacks, and CDK's LogRetention provider setting retention on log groups Lambda and API Gateway create outside CloudFormation) do not support resource-level scoping.",
      [`AwsSolutions-IAM5[Resource::<SiteBucket397A1860.Arn>/*]`]:
        "Wildcard under a specific enumerated resource: object keys under a named project bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function.",
      [`AwsSolutions-IAM5[Resource::arn:<AWS::Partition>:s3:::cdk-hnb659fds-assets-${account}-${region}/*]`]:
        "BucketDeployment reads from the CDK bootstrap assets bucket; keys are content-hashed at deploy time.",
    });
  }
}
