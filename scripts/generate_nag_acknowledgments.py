#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generate deploy-cdk/lib/nag-acknowledgments.ts and docs/CDK-NAG.md.

Input: a JSON findings dump produced by parsing a clean `cdk synth --all` run
(each entry: {level, path, cfnType, ackId, rule}). Regenerate whenever new
findings appear:

  1. rm -rf deploy-cdk/cdk.out && npx cdk synth --all --quiet > synth.log 2>&1
  2. parse the log into findings JSON (see docs/CDK-NAG.md for the recipe)
  3. python3 scripts/generate_nag_acknowledgments.py findings.json

Why raw metadata instead of Validations.acknowledge(): granular cdk-nag
finding IDs (e.g. `AwsSolutions-IAM5[Resource::arn:...]`) contain `::`, which
`Validations.qualifyId` rejects (it reserves `::` for prefix separation and
throws on IDs with more than one occurrence). cdk-nag's `isAcknowledged`
matches finding IDs against the raw keys of the
`aws:cdk:acknowledged-rules` metadata, so writing that metadata directly is
the supported-by-implementation path for granular findings.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

ACCOUNT_RE = re.compile(r"\b\d{12}\b")
STACK_ENV_RE = re.compile(r"(AgenticDataIngestion-[A-Za-z]+)-([a-z0-9]+):")

# Region of the synth used to capture findings (replaced with ${region}).
#
# Derived from the findings themselves rather than hardcoded. A hardcoded region
# is silently wrong for anyone who synthesises elsewhere: the substitution below
# simply does not match, so their own region is written literally into the
# generated files in place of the placeholder, with no error and no diff anyone
# would read as a defect.
#
# The region appears in a finding id only as the region field of a regional ARN,
# which is what this matches. Global-resource ARNs (IAM, S3) carry an empty
# region field and are correctly ignored.
ARN_REGION_RE = re.compile(r"\barn:aws:[a-z0-9-]*:([a-z]{2}(?:-[a-z]+)+-\d):\d{12}")


def derive_capture_region(findings: list[dict]) -> str | None:
    """The region these findings were captured in, read from their own ARNs.

    Returns None when no finding carries a regional ARN. That is legitimate — a
    findings set made up entirely of global-resource ARNs has no region to
    substitute — and the caller skips the substitution rather than failing.

    Raises when the findings disagree. Substituting one of several regions would
    leave the others literal, which is the same silent defect as hardcoding.
    This is reachable by design rather than theoretical: the EdgeWaf stack
    deploys to us-east-1 because a CLOUDFRONT-scope WAF must live there, so a
    synth whose EdgeWaf findings carry regional ARNs yields two regions. The
    fix then is a per-stack decision — us-east-1 is a fixed AWS constraint and
    should stay literal, while the app region should become the placeholder —
    not a wider regex here.
    """
    regions = {region for f in findings for region in ARN_REGION_RE.findall(f["ackId"])}
    if not regions:
        return None
    if len(regions) > 1:
        raise SystemExit(
            "findings span more than one region: "
            f"{', '.join(sorted(regions))}. Substituting one would leave the "
            "others literal. If this is the EdgeWaf us-east-1 case, the region "
            "substitution needs to become per-stack; see this function's "
            "docstring."
        )
    return regions.pop()


STACK_KEYS = {
    "AgenticDataIngestion-Foundation": "foundation",
    "AgenticDataIngestion-Data": "data",
    "AgenticDataIngestion-Auth": "auth",
    "AgenticDataIngestion-AgentCore": "agentcore",
    "AgenticDataIngestion-Compute": "compute",
    "AgenticDataIngestion-Orchestration": "orchestration",
    "AgenticDataIngestion-EdgeWaf": "edgeWaf",
    "AgenticDataIngestion-Hosting": "hosting",
}


def reason_for(ack_id: str) -> str:
    """Map a (normalized) granular finding ID to its written justification."""
    rules: list[tuple[str, str]] = [
        (r"IAM4\[Policy::.*AWSLambdaBasicExecutionRole",
         "AWS managed AWSLambdaBasicExecutionRole provides baseline CloudWatch logging for "
         "Lambda functions; acceptable for a sample — replace with customer-managed "
         "equivalents in stricter environments."),
        (r"IAM4\[Policy::.*AmazonAPIGatewayPushToCloudWatchLogs",
         "AWS managed AmazonAPIGatewayPushToCloudWatchLogs is the standard account-level "
         "role API Gateway requires to write execution/access logs."),
        (r"IAM5\[Action::s3:(GetObject|GetBucket|List|Abort|DeleteObject)\*",
         "CDK grantRead/grantReadWrite emit prefixed S3 action groups; every statement is "
         "resource-scoped to specific project buckets."),
        (r"IAM5\[Action::glue:(Get|List|BatchGet)\*",
         "Read-only Glue discovery; the same statement prefix-scopes resources to project "
         "naming forms."),
        (r"IAM5\[Action::glue:\*",
         "Glue lifecycle admin for runtime-created pipeline resources; the same statement "
         "prefix-scopes resources to agentic_data_ingestion*/agentic-data-ingestion-*."),
        (r"IAM5\[Action::(emr:(Describe|List)\*|athena:\*)",
         "Analytics MCP server read/query surface; Athena results land in scoped buckets "
         "and EMR access is read-only."),
        (r"IAM5\[Action::s3tables:(Get|List)\*",
         "Read-only S3 Tables discovery for the MCP server runtimes, which run with "
         "write access disabled; resources are the account's table buckets."),
        (r"IAM5\[Action::s3tables:\*",
         "S3 Tables (Iceberg) tables are created per pipeline at runtime; the API is "
         "namespaced per table bucket so actions cannot be enumerated at deploy time."),
        (r"IAM5\[Resource::\*\]",
         "The actions in this statement (X-Ray tracing, CloudWatch PutMetricData, Bedrock "
         "model invocation, ECR auth token, Step Functions SendTaskSuccess/Failure "
         "task-token callbacks, and CDK's LogRetention provider setting retention on log "
         "groups Lambda and API Gateway create outside CloudFormation) do not support "
         "resource-level scoping."),
        (r"IAM5\[Resource::<[A-Za-z0-9]+\.Arn>(/|:)",
         "Wildcard under a specific enumerated resource: object keys under a named project "
         "bucket, or :* qualified-ARN suffix emitted by grantInvoke for a named function."),
        (r"IAM5\[Resource::.*:ExportsOutputFnGetAtt.*Arn.*:\*",
         "grantInvoke emits <functionArn>:* to cover qualified ARNs (versions/aliases) of "
         "the specific cross-stack function."),
        (r"IAM5\[Resource::.*:ExportsOutputFnGetAtt.*(/index/\*|/\*)",
         "Wildcard under a specific cross-stack resource: GSIs of a named DynamoDB table "
         "or object keys under a named bucket."),
        (r"IAM5\[Resource::arn:<AWS::Partition>:s3:::cdk-hnb659fds-assets",
         "BucketDeployment reads from the CDK bootstrap assets bucket; keys are "
         "content-hashed at deploy time."),
        (r"IAM5\[Resource::arn:aws:dynamodb:.*table/agentic_data_ingestion\*",
         "Project-prefix-scoped DynamoDB tables and their GSIs."),
        (r"IAM5\[Resource::arn:aws:dynamodb:.*table/agentic_data_ingestion_[a-z_]+/index/\*",
         "GSIs of one named project table, for a statement that queries it; index "
         "names belong to the table's definition in the Data stack."),
        (r"IAM5\[Resource::arn:aws:lambda:.*function:agentic_data_ingestion_\*",
         "Project-prefix-scoped Lambda invocation — agents invoke sibling pipeline "
         "functions of this deployment."),
        (r"IAM5\[Resource::arn:aws:glue:.*:connection/\*\]",
         "glue_connection mode references pre-existing Glue Connections with "
         "arbitrary names; create_glue_pipeline reads the selected connection "
         "(and its tags) at job-creation time to resolve JDBC target "
         "credentials. Read-only (GetConnection/GetConnections/GetTags) and "
         "connection names are unknown at deploy time, so the read is "
         "account-wide; connection secrets remain protected by Secrets Manager. "
         "The dashboard API's wizard-time glue:CreateConnection/TagResource "
         "share this connection/* wildcard (the connection name does not yet "
         "exist at creation). The Glue discovery/runtime crawler execution role "
         "(GluePipelineRole) holds the same account-wide read for a separate "
         "reason: Glue validates a crawler's JdbcTargets[].ConnectionName by "
         "retrieving the connection as the PASSED role, not as the caller, so "
         "without it CreateCrawler fails with 'InvalidInputException: Unable to "
         "retrieve jdbc connection.' for every connection outside the "
         "agentic-data-ingestion-* prefix. Connection WRITE actions "
         "(Create/Update/DeleteConnection) stay prefix-scoped on both roles."),
        (r"IAM5\[Resource::arn:aws:glue:.*:session/\*\]",
         "Glue interactive sessions verify AI-generated PySpark on real Spark; "
         "the session id is minted per verification run, so the ARN cannot be "
         "enumerated at deploy time. The session itself runs under the "
         "read-only AgenticDataIngestion-VerificationSession-Role, which holds "
         "no write action of any kind."),
        (r"IAM5\[Resource::arn:aws:glue:.*(job|crawler|connection|database|table)/agentic",
         "Pipeline-scoped Glue resources are created at runtime per pipeline; ARNs cannot "
         "be enumerated at deploy time, so they are prefix-scoped to the project naming "
         "forms (snake for databases/tables, kebab for jobs/crawlers/connections)."),
        (r"IAM5\[Resource::arn:aws:glue:.*database/\*",
         "Read-only catalog browsing (GetDatabase/GetTable) for source discovery across "
         "the account catalog."),
        (r"IAM5\[Resource::arn:aws:glue:.*table/\*/\*",
         "Read-only catalog browsing (GetTable) for source discovery across the account "
         "catalog."),
        (r"IAM5\[Resource::arn:aws:glue:.*connection/\*",
         "Wizard-time Glue connection creation runs before the connection name exists; "
         "glue:TagResource additionally requires catalog+connection wildcards."),
        # Round-1 (threat model T-03) entries, first written by hand into the
        # generated module; carried here so a regeneration reproduces them. The
        # specific -ExecutionRole and boundary rules must precede the generic
        # role, ec2 and glue rules they would otherwise fall into.
        (r"IAM5\[Resource::arn:aws:iam::.*role/AgenticDataIngestion-\*-ExecutionRole\]",
         "Per-pipeline Glue execution roles are created at runtime under the "
         "AgenticDataIngestion- prefix with an -ExecutionRole suffix. iam:CreateRole and "
         "iam:PutRolePolicy are condition-scoped to the GlueJobRoleBoundary permissions "
         "boundary, and iam:PassRole to glue.amazonaws.com (threat model T-03)."),
        (r"IAM5\[Resource::arn:aws:dynamodb:.*table/agentic_data_ingestion_\*\]",
         "GlueJobRoleBoundary (permissions boundary, grants nothing): caps per-pipeline "
         "Glue roles at GetItem/PutItem on this deployment's tables (run state and JDBC "
         "watermarks)."),
        (r"IAM5\[Resource::arn:aws:ec2:\*:\*:network-interface/\*\]",
         "GlueJobRoleBoundary (permissions boundary, grants nothing): Glue tags the VPC "
         "ENIs it creates for JDBC connections; the statement is conditioned on the "
         "aws-glue-service-resource tag key."),
        (r"IAM5\[Resource::arn:aws:glue:.*:dataQualityRuleset/\*\]",
         "GlueJobRoleBoundary (permissions boundary, grants nothing): EvaluateDataQuality "
         "in a Glue ETL job publishes against a service-named ruleset resource; this is "
         "the documented minimum for a data quality ETL job."),
        (r"IAM5\[Resource::arn:aws:secretsmanager:.*:secret:\*\]",
         "GlueJobRoleBoundary (permissions boundary, grants nothing): a JDBC credential is "
         "whatever secret its Glue connection's SECRET_ID names, which the operator "
         "chooses; each per-pipeline role's inline policy grants only that one secret ARN."),
        (r"IAM5\[Resource::arn:aws:iam::.*role/AgenticDataIngestion-",
         "Per-pipeline Glue execution roles are created at runtime under the "
         "AgenticDataIngestion- prefix; iam:PassRole is condition-scoped to "
         "glue.amazonaws.com."),
        (r"IAM5\[Resource::arn:aws:events:.*(event-bus|rule)/agentic-data-ingestion-",
         "Project-prefix-scoped EventBridge bus and per-pipeline schedule rules created "
         "at runtime."),
        (r"IAM5\[Resource::arn:aws:logs:.*/aws-glue/jobs/\*",
         "Glue writes job logs to the service-owned /aws-glue/jobs/* log groups whose "
         "stream names contain runtime job-run ids."),
        (r"IAM5\[Resource::arn:aws:logs:.*/aws-glue/\*",
         "Glue writes interactive-session logs to service-owned /aws-glue/* log groups "
         "whose names carry a runtime session id, so the group cannot be enumerated at "
         "deploy time. The verification session role holds CreateLogStream/PutLogEvents "
         "only — no CreateLogGroup and no read."),
        (r"IAM5\[Resource::arn:aws:logs:.*(/aws/bedrock-agentcore/|/aws/spans/)",
         "AgentCore runtime and OTEL span log groups are created by the service per "
         "runtime/session."),
        (r"IAM5\[Resource::arn:aws:bedrock-agentcore:",
         "AgentCore runtime/memory/code-interpreter resource ids are generated by the "
         "service; prefix-scoped where the service exposes stable prefixes."),
        (r"IAM5\[Resource::arn:aws:cloudwatch:.*:alarm:AgenticDataIngestion-Critical-",
         "Critical-alert alarms are created at runtime by the notification service's "
         "CloudWatch channel (one per alerting metric); alarm names are prefix-scoped "
         "to AgenticDataIngestion-Critical-."),
        (r"IAM5\[Resource::arn:aws:ec2:.*network-interface/\*",
         "Glue JDBC connections attach ENIs with runtime-generated ids in this "
         "account/region."),
        (r"IAM5\[Resource::arn:aws:execute-api:.*@connections/\*",
         "WebSocket connection ids are runtime values; scoped to the API's @connections "
         "management path."),
        (r"IAM5\[Resource::arn:aws:s3:::\*(--table-s3|-s3tablemetadata-)",
         "S3 Tables service-managed underlying buckets follow service naming with "
         "runtime-generated suffixes."),
        (r"IAM5\[Resource::arn:aws:s3tables:.*:(bucket|table)/\*",
         "S3 Tables table buckets and tables are created per pipeline at runtime; ids "
         "cannot be enumerated at deploy time."),
        (r"IAM5\[Resource::arn:aws:s3:::adi-\*",
         "Project buckets follow the adi-*-{account}-{region} convention; the wildcard "
         "spans only this deployment's nine project buckets."),
        (r"IAM5\[Resource::arn:aws:s3:::aws-athena-query-results-\*",
         "Athena's default results buckets are account-scoped with service-generated "
         "names."),
        (r"IAM5\[Resource::arn:aws:secretsmanager:.*:secret:agentglue/test-fixtures/",
         "Legacy P18 JDBC test-fixture credentials remain under the pre-rename "
         "agentglue/test-fixtures/ path; the Glue discovery/runtime crawler role "
         "must read them to run JDBC crawls. Secrets Manager ARNs additionally "
         "carry random six-char suffixes. Remove once these fixtures are renamed "
         "to the agentic-data-ingestion/ prefix."),
        (r"IAM5\[Resource::arn:aws:secretsmanager:.*:secret:agentic-data-ingestion/",
         "Per-pipeline JDBC credentials are stored under the project's secret path at "
         "runtime; Secrets Manager ARNs additionally carry random six-char suffixes."),
        (r"IAM5\[Resource::arn:aws:sqs:.*:agentic-data-ingestion-",
         "Project-prefix-scoped SQS queues."),
        (r"IAM5\[Resource::arn:aws:states:.*:(stateMachine|execution):agentic-data-ingestion-",
         "Project-prefix-scoped Step Functions state machine and its runtime-named "
         "executions."),
    ]
    for pat, reason in rules:
        if re.search(pat, ack_id):
            return reason
    raise SystemExit(f"UNMAPPED finding id — add a justification rule: {ack_id}")


def normalize(ack_id: str, capture_region: str | None) -> str:
    out = ACCOUNT_RE.sub("${account}", ack_id)
    if capture_region:
        out = out.replace(capture_region, "${region}")
    out = STACK_ENV_RE.sub(r"\1-${environment}:", out)
    return out


FINDING_RE = re.compile(
    r"^(ERROR|WARNING) (.*?)\n   (\S+) (\S+)\n   Acknowledge with '([^']+)'",
    re.M | re.S,
)


def parse_findings(path: str) -> list[dict]:
    """Parse a raw `cdk synth` log OR a pre-parsed findings JSON file."""
    text = open(path).read()
    if path.endswith(".json"):
        return json.load(open(path))
    rows = FINDING_RE.findall(text)
    return [
        {"level": r[0], "path": r[2], "cfnType": r[3], "ackId": r[4]}
        for r in rows
    ]


def main() -> None:
    findings_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/nag-findings.json"
    findings = parse_findings(findings_path)
    print(f"parsed {len(findings)} findings from {findings_path}")
    granular = [f for f in findings if "[" in f["ackId"]]

    # Scoped to the granular findings because those are the only ones normalized;
    # a region appearing solely in a finding that is never rewritten is not an
    # ambiguity worth failing on.
    capture_region = derive_capture_region(granular)
    print(f"capture region: {capture_region or 'none detected (no regional ARNs)'}")

    per_stack: dict[str, dict[str, str]] = defaultdict(dict)
    doc_rows: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for f in granular:
        stack_full = f["path"].split("/")[0]
        base = stack_full.rsplit("-", 1)[0]
        key = STACK_KEYS[base]
        norm = normalize(f["ackId"], capture_region)
        per_stack[key][norm] = reason_for(norm)
        doc_rows[key][norm].append(f["path"].split("/", 1)[1])

    # ── emit TypeScript module ────────────────────────────────────────
    ts_path = REPO / "deploy-cdk" / "lib" / "nag-acknowledgments.ts"
    lines: list[str] = []
    lines.append("// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.")
    lines.append("// SPDX-License-Identifier: MIT-0")
    lines.append("// GENERATED FILE — do not edit by hand.")
    lines.append("// Regenerate with: python3 scripts/generate_nag_acknowledgments.py")
    lines.append("// (procedure in docs/CDK-NAG.md). Every entry is a granular cdk-nag")
    lines.append("// finding ID with a written justification (publication-cleanup R4.3/R4.5).")
    lines.append("//")
    lines.append("// WHY raw metadata: Validations.acknowledge() rejects granular IDs (their")
    lines.append("// '[Resource::...]' payload contains '::', which qualifyId reserves), but")
    lines.append("// cdk-nag's isAcknowledged() matches finding IDs against the raw keys of")
    lines.append("// the aws:cdk:acknowledged-rules metadata — so we write that key directly.")
    lines.append("import * as cdk from 'aws-cdk-lib';")
    lines.append("")
    lines.append("export interface NagAcknowledgmentStacks {")
    for key in STACK_KEYS.values():
        lines.append(f"  readonly {key}?: cdk.Stack;")
    lines.append("}")
    lines.append("")
    lines.append("/** Apply granular cdk-nag acknowledgments to each stack (no-op for absent stacks). */")
    lines.append("export function applyNagAcknowledgments(stacks: NagAcknowledgmentStacks, environment: string): void {")
    lines.append("  void environment; // used inside generated template literals")
    for key in STACK_KEYS.values():
        entries = per_stack.get(key)
        if not entries:
            continue
        lines.append(f"  if (stacks.{key}) {{")
        lines.append(f"    const account = stacks.{key}.account;")
        lines.append(f"    const region = stacks.{key}.region;")
        lines.append("    void account; void region;")
        lines.append(f"    stacks.{key}.node.addMetadata(cdk.Validations.ACKNOWLEDGED_RULES_METADATA_KEY, {{")
        for ack_id, reason in sorted(entries.items()):
            lines.append(f"      [`{ack_id}`]:")
            lines.append(f"        {json.dumps(reason)},")
        lines.append("    });")
        lines.append("  }")
    lines.append("}")
    ts_path.write_text("\n".join(lines) + "\n")
    print(f"wrote {ts_path} ({sum(len(v) for v in per_stack.values())} acknowledgments)")

    # ── emit docs/CDK-NAG.md ─────────────────────────────────────────
    doc_path = REPO / "docs" / "CDK-NAG.md"
    d: list[str] = []
    d.append("# cdk-nag Suppression Inventory")
    d.append("")
    d.append("AwsSolutionsChecks runs on every synth (disable locally with `-c adi:skipNag=true`).")
    d.append("Findings are either fixed or acknowledged with a written justification. This file is")
    d.append("generated together with `deploy-cdk/lib/nag-acknowledgments.ts` by")
    d.append("`scripts/generate_nag_acknowledgments.py` — regenerate both when findings change:")
    d.append("")
    d.append("```bash")
    d.append("cd deploy-cdk && rm -rf cdk.out")
    d.append("npx cdk synth --all --quiet > /tmp/synth.log 2>&1 || true")
    d.append("python3 ../scripts/generate_nag_acknowledgments.py /tmp/synth.log")
    d.append("# review the regenerated lib/nag-acknowledgments.ts + this file, then re-synth to verify exit 0")
    d.append("```")
    d.append("")
    d.append("## Fixed (not suppressed)")
    d.append("")
    d.append("| Rule | Resource | Fix |")
    d.append("|------|----------|-----|")
    d.append("| AwsSolutions-SF1 | Orchestration state machine | Log level raised to `ALL` (retention capped at 1 month) |")
    d.append("| AwsSolutions-S10 / SQS4 / SNS3 / SF2 | buckets, queues, topics, state machine | `enforceSSL` / TLS-only policies / X-Ray tracing (pre-existing fixes) |")
    d.append("| AwsSolutions-CFR3 | Hosting distribution | Access logs to a private, TLS-only, 30-day bucket outside the `adi-*` grant family (security review S9) |")
    d.append("")
    d.append("## Whole-rule acknowledgments (app level, `bin/agentic_data_ingestion.ts`)")
    d.append("")
    d.append("Suppressed via `Validations.of(app).acknowledge(...)` — see the justification strings")
    d.append("in the bin app. Rules: IAM4*, L1, S1, SQS3, APIG1, APIG2, APIG4, CFR1, CFR4,")
    d.append("COG2, COG8, SMG4, DDB3.")
    d.append("")
    d.append("*IAM4/IAM5 emit granular per-finding IDs that whole-rule acknowledgments cannot match;")
    d.append("those are covered by the generated per-stack acknowledgments below.")
    d.append("")
    d.append("## Granular acknowledgments (per stack, generated)")
    d.append("")
    d.append("IDs are parameterized: `${account}`/`${region}`/`${environment}` resolve per deployment.")
    d.append("")
    for key in STACK_KEYS.values():
        if key not in doc_rows:
            continue
        d.append(f"### {key}")
        d.append("")
        d.append("| Finding ID | Justification | Resources |")
        d.append("|------------|---------------|-----------|")
        for ack_id in sorted(doc_rows[key]):
            reason = per_stack[key][ack_id]
            res = "; ".join(sorted(doc_rows[key][ack_id])[:3])
            extra = len(doc_rows[key][ack_id]) - 3
            if extra > 0:
                res += f" (+{extra} more)"
            esc = ack_id.replace("|", "\\|")
            d.append(f"| `{esc}` | {reason} | {res} |")
        d.append("")
    doc_path.write_text("\n".join(d) + "\n")
    print(f"wrote {doc_path}")


if __name__ == "__main__":
    main()
