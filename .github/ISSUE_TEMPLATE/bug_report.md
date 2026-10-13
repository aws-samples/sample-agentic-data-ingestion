---
name: Bug report
about: Something in the sample does not behave as documented
title: ''
labels: bug
assignees: ''
---

**Before you start:** this is sample code provided AS-IS with no AWS Support
coverage. See [SUPPORT.md](../../SUPPORT.md). For a security vulnerability, do
not open an issue — follow [SECURITY.md](../../SECURITY.md).

## What happened

<!-- What you observed. -->

## What you expected

<!-- And where the docs led you to expect it, if applicable. -->

## Reproduction

<!--
Steps, or the pipeline configuration involved. Please redact account IDs,
ARNs and any credentials before pasting.
-->

1.
2.
3.

## Environment

| | |
|---|---|
| AWS region | |
| Node version | |
| Python version | |
| CDK version | |
| Deployed stacks | all / subset (which) |
| Source type | S3 upload / JDBC |
| Target type | S3 Tables / Glue Catalog / JDBC / S3 Parquet |

## Logs or error output

<!--
Relevant excerpt only. REDACT account IDs, ARNs, bucket names and any
credentials before pasting.
-->

```
```

## Have you checked

- [ ] [docs/TROUBLESHOOTING.md](../../docs/TROUBLESHOOTING.md)
- [ ] Both regions were bootstrapped (the app region and `us-east-1`)
- [ ] Stacks were deployed in order: Auth → Compute → Orchestration → EdgeWaf → Hosting
