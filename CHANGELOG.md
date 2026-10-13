# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.1.0] — Initial public release

First public release of the Agentic Data Ingestion reference architecture.

No earlier entries exist. This project was developed in a private repository and
is published here starting from its initial public release, so there is no
pre-0.1.0 changelog to carry over.

### Added

- AI-assisted ETL pipeline generation on AWS Glue, driven by Amazon Bedrock and
  Strands Agents, with Model Context Protocol (MCP) servers exposing AWS Glue,
  Athena, EMR and S3 Tables operations to the agents.
- Source types: S3 upload and JDBC (full-table, and watermark-based
  incremental).
- Target types: S3 Tables (Iceberg), JDBC, and S3 Parquet.
- A Cloudscape React dashboard for pipeline creation, schema mapping review,
  human approval gates, run history and cost recommendations.
- AWS Step Functions orchestration with `waitForTaskToken` approval gates and
  Glue job completion callbacks.
- Autonomous agents for self-healing failed Glue jobs, resolving low-confidence
  schema mappings, data quality investigation, and weekly cost optimisation.
- Inline AWS Glue Data Quality evaluation on all pipeline templates.
- AWS CDK v2 infrastructure across eight stacks, with cdk-nag applied.
- Amazon Cognito authentication, an Amazon CloudFront distribution with
  AWS WAF at both edge and regional scope, and CloudFront-only API access.
