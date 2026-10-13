# Agentic Data Ingestion Testing Guide

This guide is self-contained: everything needed to choose, run, and extend a test lives
here.

**It states no test counts, timings, or lint/type-check baselines, and that is deliberate.**
Every such figure is a measurement of one tree at one moment. Writing one down creates a
second copy that rots while reading as authoritative, and a reader who trusts a stale
literal is worse off than one who runs the command. So each section below carries **the
command that produces the figure** instead of the figure. Run it.

## Test Tiers — pick the tier by kind of change

The full suite is the wrong per-task instrument. It is slow enough that you will skip it,
and unreliable when two runs overlap. Choose by **what kind of change you made**, not only
by which directory moved.

| Tier | When | Run |
|---|---|---|
| **1** | Every change | `make lint`, `make format-check`. Plus `pytest tests/cdk/` if anything under `deploy-cdk/` moved |
| **2** | Per task, scoped to blast radius | `make test-scope DIR=<dir>` — the directory mapping to what you changed, per [Code-to-test mapping](#code-to-test-mapping) below |
| ↳ | You added, moved, or renamed a test file | `make test-meta` — **must run alone** |
| ↳ | `dashboard-react/` changed | `make test-frontend` — runs vitest **then** `tsc --noEmit`; both legs gate |
| **3** | Phase boundary or checkpoint task | `make test-fast` — **must run alone** |
| **4** | Before commit / end of work | `make check`, then `make test-cdk` and `make test-frontend` |
| **never a gate** | — | `make test-ci`, `make test-e2e` — see below |

`make check` is `name-check` + `lint` + `type-check` + `security` + `test-fast`.
`make help` lists every target.

### Why `make test-fast` and `make test-meta` must run alone

This is a concurrency rule, not a bug to fix.

The layers run as separate pytest processes, and `tests/unit/test_marker_selection_prop.py`
writes a stub test file into **all five** layer directories for the duration of its run. Any
second pytest process that observes that window fails `test_hierarchy_placement_prop.py`,
because the stub basename now appears in five layers at once, and can fail the marker test
itself when the other process's cleanup removes the stub mid-assertion.

Within one sequential process it is harmless: no test executes while the stubs exist.
`make test-meta` writes the same stubs, so it carries the same rule.

A checkpoint gate run against a moving tree also measures nothing. A pass tells you the tree
was green at no particular moment, and a failure cannot be attributed to a change.

### Why `make test-ci` and `make test-e2e` are never gates

`make test-ci` carries known pre-existing failures owned by other work. It is
informational, never a pass/fail signal.

`make test-e2e` runs against **real AWS**, and two of the `tests/e2e/test_glue_*.py` tests
create **billable** Glue interactive sessions.

### Every other test target runs without AWS credentials

The Makefile's `NO_AWS_CREDS` prefix unsets `AWS_PROFILE` and the key variables, points the
config and credentials files at `/dev/null`, and disables the EC2 metadata endpoint for
every pytest target except `test-e2e`, and for `test-cdk`. A unit test once reached real
DynamoDB with the developer's own credentials and passed, then failed on a machine without
them. A test that needs AWS now fails the same way everywhere.

### Several gates exit non-zero on a clean tree

Read these as "no worse than", not as "green". The tree carries a style and typing backlog,
and a gate that fails on every run gets ignored rather than fixed.

| Target | Invocation | How to read it |
|---|---|---|
| `make lint` | `uv run ruff check lambda/ shared/ mcp/ tests/` | **Advisory and stays advisory.** Its recipe ends in `\|\| echo`, so it cannot fail `make check`. Do not remove that guard to make it gateable. Read it scoped to your own edits |
| `make format-check` | `uv run ruff format --check lambda/ shared/ mcp/ tests/` | A number of files are pre-existing unformatted. Check **your** files: `uv run ruff format --check <your files>`. Do not run `make format`, which reformats files you did not touch |
| `make type-check` | `uv run mypy shared/` plus `MYPYPATH=shared uv run mypy lambda/` | Two legs with different strictness. `--no-error-summary` means mypy prints no total, so count `: error:` lines per leg. Quote the target, not a hand-rolled `mypy lambda/`, which reports differently because it lacks `MYPYPATH` |
| `make security` | `uv run bandit -r lambda/ shared/ mcp/ agentcore/ -c pyproject.toml -q` | Pre-existing Low/Medium/High findings. Compare against a prior run rather than expecting zero |
| `make name-check` | `make name-check` | Blocking **when present**. The scanner it runs is internal-only tooling that lives outside this project, so in a published checkout the target reports that it skipped and succeeds. There are no internal names in a public copy for it to find. |

To establish the current state of any row, run the invocation in the middle column. To
decide whether *your* change made something worse, run it before and after.

## Code-to-test mapping

The directory a tier-2 scoped run should target. A row reading **no tests** has no tier-2
gate at all: `make test-scope` on it collects nothing and still exits 0, so a change there
needs a test written, or `make test-fast` as the real gate.

`tests/unit/test_test_mapping_completeness_prop.py` diffs the Lambda table below against the
filesystem in both directions, so it cannot silently go stale.

### Lambda handlers

| Lambda directory | Test directory |
|---|---|
| `lambda/agentic_data_ingestion_agent_dispatch/` | `tests/unit/lambdas/agent_dispatch/` |
| `lambda/agentic_data_ingestion_pipeline_trigger/` | `tests/unit/lambdas/pipeline_trigger/` |
| `lambda/agentic_data_ingestion_file_validation/` | `tests/unit/lambdas/file_validation/` |
| `lambda/agentic_data_ingestion_schema_extract/` | `tests/unit/lambdas/schema_extract/` |
| `lambda/agentic_data_ingestion_schema_compare/` | `tests/unit/lambdas/schema_compare/` |
| `lambda/agentic_data_ingestion_schema_mapping/` | `tests/unit/lambdas/schema_mapping/` |
| `lambda/agentic_data_ingestion_create_code/` | `tests/unit/lambdas/create_code/` |
| `lambda/agentic_data_ingestion_create_infrastructure/` | `tests/unit/lambdas/create_infrastructure/` |
| `lambda/agentic_data_ingestion_create_glue_pipeline/` | `tests/unit/lambdas/create_glue_pipeline/` |
| `lambda/agentic_data_ingestion_create_quality_checks/` | `tests/unit/lambdas/create_quality_checks/` |
| `lambda/agentic_data_ingestion_run_pipeline/` | `tests/unit/lambdas/run_pipeline/` |
| `lambda/agentic_data_ingestion_job_completion/` | `tests/unit/lambdas/job_completion/` |
| `lambda/agentic_data_ingestion_record_failure/` | `tests/unit/lambdas/record_failure/` |
| `lambda/agentic_data_ingestion_queue_loss_recorder/` | `tests/unit/lambdas/queue_loss_recorder/` |
| `lambda/agentic_data_ingestion_self_healing_dispatch/` | `tests/unit/lambdas/self_healing_dispatch/` |
| `lambda/agentic_data_ingestion_execution_updater/` | `tests/unit/lambdas/execution_updater/` |
| `lambda/agentic_data_ingestion_dashboard_api/` | `tests/unit/lambdas/dashboard_api/` |
| `lambda/agentic_data_ingestion_approval_handler/` | `tests/unit/lambdas/approval_handler/` |
| `lambda/agentic_data_ingestion_approval_config/` | **no tests** — the directory exists but holds only an empty `__init__.py`. See the note below |
| `lambda/agentic_data_ingestion_approval_timeout_handler/` | `tests/unit/lambdas/approval_timeout/` |
| `lambda/agentic_data_ingestion_test_code/` | `tests/unit/lambdas/test_code/` |
| `lambda/agentic_data_ingestion_ws_authorizer/` | `tests/unit/lambdas/ws_authorizer/` |
| `lambda/agentic_data_ingestion_ws_broadcast/` | `tests/unit/lambdas/ws_broadcast/` |
| `lambda/agentic_data_ingestion_ws_connect/` | `tests/unit/lambdas/ws_connect/` |
| `lambda/agentic_data_ingestion_ws_disconnect/` | `tests/unit/lambdas/ws_disconnect/` |

> **`approval_config` — why the row stays with a gap named in it.** `tests/unit/lambdas/approval_config`
> exists and contains only an empty `__init__.py`, so there is no `test_*.py` to collect and a
> scoped run exits 0 having verified nothing. Dropping the row was rejected because it leaves
> the same silence with nothing pointing at it, and adding placeholder tests was rejected
> because a green gate with no coverage is worse than a named gap. Writing real tests closes it.

### AgentCore agents

Without these rows an agent edit resolves to no tier-2 gate. The four pipeline agents share
their Lambda counterpart's test directory; the five autonomous agents have their own.

| AgentCore directory | Test directory |
|---|---|
| `agentcore/create_code/` | `tests/unit/lambdas/create_code/` |
| `agentcore/create_glue_pipeline/` | `tests/unit/lambdas/create_glue_pipeline/` |
| `agentcore/create_infrastructure/` | `tests/unit/lambdas/create_infrastructure/` |
| `agentcore/test_code/` | `tests/unit/lambdas/test_code/` |
| `agentcore/self_healing_agent/` | `tests/unit/agents/self_healing_agent/` |
| `agentcore/complex_mapping_agent/` | `tests/unit/agents/complex_mapping_agent/` |
| `agentcore/cost_optimization_agent/` | `tests/unit/agents/cost_optimization_agent/` |
| `agentcore/data_quality_agent/` | `tests/unit/agents/data_quality_agent/` |
| `agentcore/nl_pipeline_builder/` | `tests/unit/agents/nl_pipeline_builder/` |

Editing one of the four pipeline agents usually means editing its Lambda directory too, since
both stacks deploy, and one scoped run covers both sides.

### Shell scripts

These are single files, so the gate is `make test-scope DIR=tests/unit/test_<file>.py`. The
pure computations live in sourceable libraries and the entrypoints are thin drivers, so
pytest calls the library functions directly and lifts non-function driver regions verbatim to
run them against an argv-logging `aws` stub on a hermetic PATH.

| Shell script | Test file |
|---|---|
| `deploy.sh` | `tests/unit/test_deploy_interpreter_probe.py`, `tests/unit/test_deploy_bootstrap_targets.py` (+`_prop`) |
| `scripts/deploy_prereq_lib.sh` | `tests/unit/test_deploy_stack_verification.py` (+`_prop`), and the two above |
| `destroy.sh` | `tests/unit/test_destroy_lib.py` |
| `scripts/destroy_lib.sh` | `tests/unit/test_destroy_lib.py`, `tests/unit/test_destroy_stack_set.py` |
| `scripts/cleanup_agentcore_runtimes.sh` | `tests/unit/test_destroy_lib.py` |
| `scripts/prune_cdk_out.sh` | **no tests** — linted only, and only by `make shellcheck` |

> **The two shellcheck gates cover different sets, and neither covers every script.**
> `test_shellcheck_is_clean` runs over the five `SHELL_SOURCES` and **skips entirely when
> shellcheck is not installed**, because shellcheck is a system binary outside the uv
> lockfile. `make shellcheck` runs unskippably over six, adding `prune_cdk_out.sh`. Read a
> green `make check` accordingly: **it does not run shellcheck at all.**
>
> Unlike the Lambda table, this one is **not** machine-checked, so it can go stale silently.

## What the Tests Cover

- **Unit tests** — each Lambda handler in isolation with moto mocks.
- **Contract tests** — JSON Schema validation of API responses against the TypeScript
  interfaces, and event payloads against the Step Functions contracts.
- **Integration tests** — multi-Lambda flows end-to-end with moto: pipeline trigger → Step
  Functions → completion, approval lifecycles, concurrent access, format handling.
- **Property tests** — hypothesis-based: schema round-trips, parameter builder invariants,
  artifact schema validation, idempotency, mapping context round-trips.
- **CDK tests** — two separate suites. `pytest tests/cdk/` (Python) and `make test-cdk`
  (vitest, in `deploy-cdk/test/`). They are not the same tests; run both before committing.

## Directory Structure

```
tests/
├── unit/                    # Fast isolated unit tests (moto)
│   ├── lambdas/             # One subdir per Lambda handler
│   ├── shared/              # Shared utility tests
│   └── templates/           # PySpark template tests
├── contracts/               # API & event schema contract tests
│   ├── api_responses/       # JSON Schema validation for API endpoints
│   └── event_schemas/       # Event payload validation
├── integration/             # Multi-Lambda flow tests (moto)
│   ├── pipeline_flow/       # Trigger → SFN → completion flows
│   ├── approval_flow/       # Approval lifecycle tests
│   ├── concurrency/         # Deduplication and concurrent access
│   ├── performance/         # N+1 query regression detection
│   ├── migration/           # Schema migration safety
│   ├── websocket/           # WebSocket broadcast path
│   └── multi_format/        # CSV/JSON/Parquet format handling
├── e2e/                     # Real AWS tests (skipped by default)
│   ├── smoke/               # CSV/JSON/Parquet pipeline smoke tests
│   ├── concurrency/         # Multi-file deduplication E2E
│   └── checkpoint/          # Checkpoint/resume E2E
├── fixtures/                # Shared test data and fixtures
│   ├── conftest.py          # Shared fixtures (moto/real-AWS toggle)
│   ├── data/                # Canonical CSV/JSON/Parquet files
│   ├── dynamodb/            # DynamoDB item snapshots
│   ├── api_responses/       # Expected API response JSON
│   ├── events/              # Sample event payloads
│   └── golden_scripts/      # Golden PySpark rendered scripts
└── cdk/                     # CDK assertion tests (Python/pytest — the vitest
                             #   ones live in deploy-cdk/test/)
```

Top-level `tests/unit/*.py` files are the cross-layer meta-tests (`make test-meta`). They
assert placement and marker rules across all five layers, so a scoped directory run never
reaches them.

## Adding New Tests

When adding or modifying a Lambda handler:

- **Unit tests** in `tests/unit/lambdas/<handler_name>/`.
- **Error-path tests** in that directory's `test_error_paths.py` — malformed input and AWS
  exceptions. Several handlers already have one; follow their shape.
- **Idempotency tests** if the handler has side effects.
- **Contract tests** in `tests/contracts/api_responses/` if it returns API responses, or
  `tests/contracts/event_schemas/` if it consumes events. Schema definitions live in each of
  those directories' `schemas.py`.

### Naming conventions

- Test files: `test_<module>_<behavior>.py`
- Test functions: `test_<behavior>_<scenario>`
- Property tests: `test_<name>_prop.py`, placed in the layer directory of the code under
  test. Use hypothesis with `@settings(max_examples=10)` to keep them fast.

### Fixture patterns

- Use the shared fixtures in `tests/fixtures/conftest.py`. **Do not** create your own
  `moto.mock_aws()` or `boto3.client` mocks for DynamoDB, S3, or SQS.
- Load sample data from `tests/fixtures/data/`, `tests/fixtures/events/`, and siblings.
- Use the `mock_lambda_env` fixture to set Lambda environment variables.

## Moto / Real-AWS Toggle

`AGENTIC_DATA_INGESTION_TEST_REAL_AWS` controls the backend:

- `false` (default) — moto in-memory mocks. No AWS credentials needed.
- `true` — real AWS. Resources are prefixed `test-{uuid[:8]}-` for isolation, and cleanup
  runs automatically in fixture teardown.

## Golden File Updates

Golden PySpark scripts in `tests/fixtures/golden_scripts/` are reference outputs. To update:

1. Render the template with the updated config
2. Review the diff
3. Replace the golden file
4. Run `make test-scope DIR=tests/unit/templates/` — scoped is enough here

Note that golden scripts deliberately carry **no licence header**. Stamping one would embed
it in every generated PySpark job.

## Pytest Markers

- `unit` — auto-applied in `tests/unit/`
- `contract` — auto-applied in `tests/contracts/`
- `integration` — auto-applied in `tests/integration/`
- `e2e` — auto-applied in `tests/e2e/`, excluded by default
- `cdk` — auto-applied in `tests/cdk/`
- `slow` — for tests exceeding time budgets

## Coverage Targets

- Lambda handlers: ≥ 80% line coverage
- Shared utilities: ≥ 90% line coverage

Generate the report with `make test-coverage`.

## Known Limitations

**`sys.modules` caching between test layers.** Python's module caching causes
cross-contamination when unit, contract, and integration tests run in a single pytest
process: moto patches applied in one layer leak into another, producing spurious failures.

*Solution:* each layer runs in its own pytest invocation. The Makefile targets handle this.
**Do not run `pytest tests/` as a single command** — use the Makefile targets.

**Integration test skips.** Some require real AWS credentials
(`AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true`) because they exercise actual boto3 calls. The
rest are legitimate conditional skips: feature flags, optional dependencies,
platform-specific behaviour.

**E2E tests** require a fully deployed stack, with the Auth stack deployed with
`-c adi:deployE2eClient=true`: the `USER_PASSWORD_AUTH` app client the harness uses exists
only with that flag, and deploying Auth without it deletes the client. They are excluded by
default and run only via `make test-e2e`. API tests authenticate with Cognito automatically:
the harness reads the Auth stack's `E2eTestClientId` output, creates a throwaway user, obtains an ID token via
`USER_PASSWORD_AUTH`, and routes requests through the CloudFront URL, because the regional
WAF blocks direct `execute-api` calls. The WebSocket test passes the token via `?token=` on
`$connect`. The `$connect` authorizer accepts a token only if its `aud` is an app client
listed in the SSM parameters named by `USER_POOL_CLIENT_ID_PARAMETERS`; the Auth stack
writes the e2e client's ID to one of them when the flag creates it. No environment variables are needed; overrides are `ADI_E2E_BASE_URL`,
`ADI_E2E_USER_POOL_ID`, `ADI_E2E_CLIENT_ID`, `ADI_E2E_ENVIRONMENT`. Pool-level required MFA
breaks the programmatic-token path, so the same `-c adi:deployE2eClient=true` that creates the
client also makes MFA optional. Do not combine it with `-c adi:requireMfa=true`, which forces MFA
back on.
