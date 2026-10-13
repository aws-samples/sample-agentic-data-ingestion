# Agentic Data Ingestion Documentation

**New here? Start with [GETTING-STARTED.md](GETTING-STARTED.md)** — the single deploy
walkthrough — then [ARCHITECTURE.md](ARCHITECTURE.md) for how the system fits together.
[../QUICKSTART.md](../QUICKSTART.md) is the terse copy-paste path and owns the teardown
procedure.

## Core

- [ARCHITECTURE.md](ARCHITECTURE.md) — System architecture, layers, data flow, security
- [WORKFLOW.md](WORKFLOW.md) — 10-step pipeline execution, approval gates, inline data quality, state tracking
- [DATABASE-ARCHITECTURE.md](DATABASE-ARCHITECTURE.md) — Table relationships, data flow patterns
- [DYNAMODB-TABLES.md](DYNAMODB-TABLES.md) — per-table schemas, GSIs, access patterns
- [LAMBDA-FUNCTIONS.md](LAMBDA-FUNCTIONS.md) — Lambda input/output contracts
- [MCP-INTEGRATION.md](MCP-INTEGRATION.md) — the focused MCP servers (codegen, infra, analytics, s3_tables, data_processing)
- [AGENTS.md](AGENTS.md) — 5 agents (self-healing, mapping, quality, cost, NL pipeline) — see the guide for which are automatically triggered
- [WELL-ARCHITECTED.md](WELL-ARCHITECTED.md) — AWS Well-Architected Framework review (6 pillars)

## User Guides

- [agentic_data_ingestion.md](agentic_data_ingestion.md) — Product overview: what it is, the problem it solves
- [GETTING-STARTED.md](GETTING-STARTED.md) — Setup, first pipeline, verification
- [DASHBOARD.md](DASHBOARD.md) — React dashboard features, components, development
- [JDBC_SOURCE.md](JDBC_SOURCE.md) — JDBC source/target pipelines and Glue connections
- [TESTING.md](TESTING.md) — Layered test suite (unit, contracts, integration, e2e, CDK)
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — Common issues and fixes

## Deployment

- [DEPLOY_HOSTING.md](DEPLOY_HOSTING.md) — Deploy the hosted dashboard + Cognito auth (8 stacks, us-east-1 EdgeWaf bootstrap, verification checklist)
- [DEPLOY_EC2.md](DEPLOY_EC2.md) — Known-good EC2 build host recipe (when local Docker isn't an option)
- [LOCAL_DEVELOPMENT.md](LOCAL_DEVELOPMENT.md) — Run the React dashboard locally against the deployed Cognito-protected API (Vite proxy → CloudFront)
- [OBSERVABILITY.md](OBSERVABILITY.md) — CloudWatch Logs Insights query recipes, error tracking, tracing, and monitoring commands
- [COST.md](COST.md) — Monthly baseline and per-pipeline-run cost estimates

## Reference

- [CDK-NAG.md](CDK-NAG.md) — cdk-nag suppressions and the acknowledgment rows, with the reason each was accepted

## Historical design records

Superseded design specs, dated pre-remediation baselines and completed improvement roadmaps are
**not part of this documentation set**. They were records of the tree as it stood when each was
written, unmaintained thereafter, and several described designs that were later changed, so keeping
them alongside current docs invited reading an old design as the live one.

For why the system is shaped the way it is, read [ARCHITECTURE.md](ARCHITECTURE.md). For current
infrastructure, read `deploy-cdk/` and the CDK stack sources. For why a specific line exists, the
git history of that file is the authority.
