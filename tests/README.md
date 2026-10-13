# Agentic Data Ingestion Test Suite

**No test counts here.** Every count this file used to state was wrong by three to ten times, because
a literal copied out of one run rots the moment a test is added. [docs/TESTING.md](../docs/TESTING.md)
is the guide: it carries the tier table, the command for each tier, the code-to-test mapping a scoped
run needs, and the reason two targets must run alone. To know how many tests exist, run them.

```bash
make help            # every target
make test-fast       # unit + contract — must run alone
make test-scope DIR=tests/unit/lambdas/<name>/   # the per-change gate
```

Layers run in separate pytest processes to avoid `sys.modules` cross-contamination between moto
patches. **Use the Makefile targets. Do not run `pytest tests/` directly** — see
[docs/TESTING.md § Known Limitations](../docs/TESTING.md#known-limitations).

## Directory Structure

```
tests/
├── unit/                    # Isolated handler tests (moto)
│   ├── lambdas/             # One subdir per Lambda handler
│   ├── agents/              # The five autonomous AgentCore agents
│   ├── agentcore/           # Container-level assertions (e.g. non-root)
│   ├── mcp/                 # MCP server tests
│   ├── shared/              # Shared utility tests
│   ├── templates/           # PySpark template tests
│   ├── scripts/             # Tests for scripts/
│   └── *.py                 # Cross-layer meta-tests (make test-meta)
├── contracts/               # API and event schema validation
│   ├── api_responses/       # JSON Schema validation for API endpoints
│   └── event_schemas/       # Event payload validation
├── integration/             # Multi-Lambda flow tests (moto)
│   ├── pipeline_flow/       # Trigger → Step Functions → completion
│   ├── approval_flow/       # Approval lifecycle
│   ├── concurrency/         # Deduplication and concurrent access
│   ├── performance/         # N+1 query regression detection
│   ├── migration/           # Schema migration safety
│   ├── websocket/           # WebSocket broadcast path
│   └── multi_format/        # CSV/JSON/Parquet handling
├── e2e/                     # Real AWS (excluded by default; two Glue tests BILL)
│   ├── smoke/               # CSV/JSON/Parquet pipeline smoke tests
│   ├── concurrency/         # Multi-file deduplication
│   └── checkpoint/          # Checkpoint and resume
├── fixtures/                # Shared fixtures and test data — READ BY TESTS
│   ├── conftest.py          # moto / real-AWS toggle
│   ├── data/                # Canonical CSV/JSON/Parquet inputs
│   ├── dynamodb/            # DynamoDB item snapshots
│   ├── api_responses/       # Expected API response JSON
│   ├── events/              # Sample event payloads
│   └── golden_scripts/      # Golden rendered PySpark scripts
├── test-data/               # Sample uploads for the getting-started walkthrough.
│                            #   NOT read by any test — tests use fixtures/data/
└── cdk/                     # CDK assertion tests — PYTHON / pytest
```

**Two CDK suites exist and they are not the same tests.** `tests/cdk/` is Python under pytest, run by
`pytest tests/cdk/`. The TypeScript assertions are in `deploy-cdk/test/` under **vitest**, run by
`make test-cdk`. An earlier revision of this file described `tests/cdk/` as "TypeScript/Jest", which
is wrong on both the language and the runner. Run both before committing.

## Adding New Tests

1. Unit tests in `tests/unit/lambdas/<function_name>/`
2. Name files `test_<module>_<behavior>.py`, functions `test_<behavior>_<scenario>`
3. Use the shared fixtures in `tests/fixtures/conftest.py`. **Do not** create your own
   `moto.mock_aws()` or `boto3.client` mocks for DynamoDB, S3 or SQS
4. Load sample data from `tests/fixtures/` rather than constructing inline dicts
5. Property tests are `test_<name>_prop.py`, placed in the layer directory of the code under test,
   using hypothesis with `@settings(max_examples=10)`
6. Error-path tests belong in that directory's `test_error_paths.py`

**If you add, move or rename a test file, run `make test-meta`** — and run it alone. The cross-layer
meta-tests assert placement and marker rules, and they write stub files into all five layer
directories while running, so a second concurrent pytest process fails them spuriously.

## Moto / Real-AWS Toggle

`AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true` runs against real AWS. Default is moto, in-memory, no
credentials needed. Real-AWS mode prefixes resources with `test-{uuid[:8]}-` for isolation and cleans
up in fixture teardown.

## Golden File Updates

Golden PySpark scripts live in `tests/fixtures/golden_scripts/`. To update:

1. Render the template with the new config
2. Review the diff carefully
3. Replace the golden file
4. Run `make test-scope DIR=tests/unit/templates/` — scoped is enough; `make test-fast` is a
   checkpoint gate, not a per-change one

Golden scripts deliberately carry **no licence header**. Stamping one would embed it into every
generated PySpark job.

## Triage Log

Tests removed during triage are recorded in [TRIAGE_LOG.md](TRIAGE_LOG.md) with the reason, the date
and the role responsible. `tests/unit/test_triage_log_completeness_prop.py` asserts that file exists,
is non-empty, and carries an entry for each known removed test.
