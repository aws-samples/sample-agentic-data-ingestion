# Security Policy

## Reporting a vulnerability

If you discover a potential security issue in this project, please notify
AWS/Amazon Security via the
[vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/)
or email <aws-security@amazon.com>.

**Please do not create a public GitHub issue for a security report.**

## Scope

This repository is sample code published for reference and adaptation. It is
not a supported AWS service and carries no service-level agreement. See
[SUPPORT.md](SUPPORT.md).

Security reports are still welcome and will be triaged, but note that:

- The sample is **not hardened for production use** as shipped. Several
  deliberately permissive defaults exist to keep the demo path short; the
  "Hardening for real data" section of [README.md](README.md) enumerates the ones
  the maintainers are aware of. A report that the defaults are permissive is
  expected rather than novel.
- Findings in third-party dependencies are best reported upstream. This project
  vendors one third-party component, documented in [NOTICE](NOTICE).

## What this sample does with untrusted input

One property is load-bearing and worth stating explicitly:

**Credentials are referenced, never copied.** Database credentials are passed by
AWS Secrets Manager ARN. Plaintext credentials are not written into job arguments
or connection properties — job arguments are visible in the Glue console, in
CloudTrail, and in job-run history, so a plaintext credential there is a leak.

If you find a path that violates that, it is a genuine finding and we would like
to hear about it.

## Threat model: model-generated code is executed

**This project uses a language model to write PySpark, and then runs it.** That is
the purpose of the sample rather than an oversight, but it means model output sits
inside the trust boundary of the Glue job. Understand this chain before pointing
the system at data you care about.

An earlier revision of this document claimed model output was "treated as data,
never as instructions". That was wrong and is corrected here rather than quietly
removed, because the correction is the useful part: the generated transform block
is assembled into a `.py` file which is assigned as a Glue job's `ScriptLocation`
and executed by Spark.

### The chain, and what actually constrains each step

| Stage | What happens | What constrains it |
|---|---|---|
| **1. Generation** | A model produces a *transform block*, not a whole program. | The block is inserted into a fixed Jinja2 template chosen by `(source_type, target_type)`. The model does not author the surrounding read, write, or session setup. |
| **2. Assembly** | `lambda/agentic_data_ingestion_create_code/code_assembly.py` renders template + block into a complete script. | Template structure, plus a static allowlist on the block (`transform_safety.py`) that runs **before** the render. The block may use only assignment, `if`, `for`, comprehensions and expressions; only `df`, `df_transformed`, the `pyspark.sql.functions`/`types` names the templates import, a few side-effect-free builtins, and names it assigns itself; and only Column/DataFrame transformation methods. Imports, `def`, `class`, `lambda`, `try`, `with`, `while`, `del`, the job's own module-scope names (`spark`, `glueContext`, `sc`, `args`, `boto3`, `sys`), every `_`-prefixed or dunder attribute, and `write`/`collect`/`rdd`/`toPandas` are rejected. Spark SQL text is scanned too: `reflect`, `try_reflect`, `java_method` and `SELECT` are refused in any string literal, and `expr()`, `filter()` and `where()` must be called directly with one argument that is a string literal, or for `filter()`/`where()` a Column expression built from those names. Text computed in any way, including by a string method such as `.replace`, is refused there. Pipeline configuration that the templates emit as code is checked at the same point: partition column names must be identifiers and are emitted as string literals, a checkpoint bucket must be a valid S3 bucket name, and the JDBC engine is emitted as an escaped string. A rejected block fails the run with a message naming the construct; no script is stored. **This is an allowlist, not a sandbox:** it does not reason about what a permitted call does with its data, and it does not cover the fallback path below. |
| **3. Approval** | A human reviews the generated code in the dashboard; Step Functions holds the execution on `waitForTaskToken`. | **This is the primary control, and it is operator-configurable.** `code_generation_enabled` is a per-pipeline flag that can be set to `false`. Doing so skips review for runs whose code passes its checks cleanly; a run with failing checks, a failing review verdict, or a regenerated transform is still held for a human. A missing or non-boolean value counts as `true`. (An earlier version of this table said `auto_approval_thresholds` could auto-approve on a code-quality score. The approval-config API still accepts that field, but nothing reads it: the score was removed, so it cannot bypass the gate.) |
| **4. Execution** | Glue runs the script. | The Glue job's IAM role. This is the real blast radius: whatever that role can reach, generated code can reach. |

### What this means for you

- **The human approval gate is the control that matters.** If you set
  `code_generation_enabled` to `false`, model output that passes its checks
  reaches execution unreviewed. Passing checks is evidence the code runs and
  produces the expected shape, not that it is safe, so treat that as a
  deliberate risk decision. The gate does not separate duties: the account
  that starts a run can also approve it, and every signed-in user can approve
  any run (see [README](README.md#hardening-for-real-data), *Role-based access*).
- **Scope the Glue job role to the data the pipeline needs**, and no more. Step 4
  is where a bad generation becomes a real incident. Each pipeline's role is
  built at run time with an inline policy scoped to its own buckets, secret,
  Glue connection and target table, and carries no AWS managed policy (the
  former `AWSGlueServiceRole` granted `glue:*` on `*`). An S3 output location
  or Redshift temp directory must name one valid bucket, so a wildcard cannot
  reach the policy: the pipelines API refuses it on create and edit, and the
  role builder refuses it again for records written earlier. The role builder
  holds an S3 Tables target's table bucket name to the same rule, though that
  target's `s3tables` grant still covers every table bucket in the account,
  for discovery. Every such role is
  created with the `GlueJobRoleBoundary` permissions boundary, and the IAM
  actions that create and edit these roles are allowed only with that boundary
  attached, so code able to run the role builder cannot mint a broader role.
  Roles created by an earlier version keep their old policy until deleted;
  see `docs/TROUBLESHOOTING.md`.
- **The agents cannot complete an approval.** Each agent runtime has its own
  IAM role, and the MCP servers share a read-only one. Every one of those roles
  is explicitly denied the DynamoDB table that holds pending approval tokens and
  the Glue completion token of each running job. Only the four pipeline-step
  runtimes can report a Step Functions task result at all. The WebSocket
  connection authorizer, the one function that parses unauthenticated input
  on an edge with no WAF, runs on its own role that can read two SSM
  parameters and write its logs, and nothing else.
- **Prompt injection is in scope.** Source data and schema field names reach the
  generation prompt. A crafted field name influencing generated code, and thereby
  what the Glue role does, is a genuine finding — please report it. The mapping,
  complex-mapping and pipeline-builder prompts tell the model to treat schema,
  sample and operator sections as data, and the assembly allowlist above refuses
  a transform block that reaches outside its frame. Neither is complete on its
  own: a prompt instruction can be overridden, and an allowlist only constrains
  the shape of the code. A block that stays inside the allowlist but transforms
  the data wrongly is what the approval gate and the verification step are for.
- **The fallback full-AI path is not covered by the allowlist.** A target type
  with no template makes `create_code` ask the model for a whole script instead
  of a transform block. That script passes syntax and anti-pattern validation
  only. Every target type the dashboard offers has a template, but the
  pipelines API does not reject other target types, so a pipeline created
  directly through the API with an unrecognised type reaches this path. Do not
  add a target type without a template if you rely on the allowlist.
- **Within the allowlist, a block can still corrupt its own pipeline's
  output.** It can drop rows, drop columns or rewrite values, and the template
  writes the result to the configured target; in `overwrite` mode that
  replaces the target's contents. Review is what catches this, which is the
  reason to keep the approval gate on for data you do not control.
- **The control plane does not execute model output.** There is no `eval` and no
  code deserialization anywhere in `lambda/`, `shared/` or `mcp/`. A path that
  adds one is a genuine finding. Note this is a narrower claim than the one it
  replaces: it is about the Lambda control plane, not about Glue, which executes
  the script by design.
