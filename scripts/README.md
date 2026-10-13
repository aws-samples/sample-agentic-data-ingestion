# Utility Scripts

Standalone scripts for maintenance, testing, and cleanup operations.

## Scripts

### cleanup_agentcore_runtimes.sh

Clears the AgentCore **runtimes** a failed teardown abandoned. `destroy.sh`'s
`--retain-resources` retry deletes a stuck stack while *abandoning* the resources
that would not go, so the runtimes stay live with nothing managing them — and
their physical names carry no environment suffix, so the next deploy collides
with them.

**Dry run by default. `--profile` and `--region` are required — no defaults, and
the resolved account is echoed before anything is read.** Even with `--delete` it
**refuses while the AgentCore CloudFormation stack still exists**: a runtime that
stack still claims is a live resource, not an orphan. The match is a literal,
start-anchored prefix on the runtime *name*, defaulting to the prefix that covers
every runtime this system declares; pass `--prefix agentic_data_ingestion_agent_`
for the agent runtimes only. Deletion uses the runtime **id** returned by
`list-agent-runtimes`, never a name and never an id composed from one.

**Not covered:** `AWS::BedrockAgentCore::Memory`. Different resource type,
different API (`list-memories`), invisible to a runtime sweep — delete a leftover
one by hand. For the runtime/agent-container counts and the split behind them,
read `docs/ARCHITECTURE.md` § System Inventory rather than copying a figure here.

The API is `bedrock-agentcore-control`. `aws bedrock-agent delete-agent` targets a
different service and cannot find these resources.

**Usage:**
```bash
scripts/cleanup_agentcore_runtimes.sh --profile <name> --region us-west-2
scripts/cleanup_agentcore_runtimes.sh --profile <name> --region us-west-2 --delete
```

### seed_mapping_memory.py

Seeds AgentCore Memory with historical approved field mappings for improved AI mapping suggestions.

**Usage:**
```bash
python seed_mapping_memory.py --dry-run   # Preview what would be seeded
python seed_mapping_memory.py             # Execute seeding
```

### detect_unindexed_executions.py

Finds execution rows absent from `RunsByTimeIndex`. That index is sparse, so a row
missing either key attribute is absent from it **silently** and will not appear in
run history. Read-only; it reports and does not repair.

### generate_nag_acknowledgments.py

Regenerates the cdk-nag acknowledgment rows from a synth log. **Read
[docs/CDK-NAG.md](../docs/CDK-NAG.md) before running it** — with zero findings it
rewrites both target files from an empty result and silently empties them.

### prune_cdk_out.sh

Deletes stale `cdk.out` asset trees. Exposed as `make prune-cdk-out`.

### `_regen_golden_snapshots.py`, `_render_jdbc_golden_snapshots.py`, `_render_jdbc_template_check.py`

Underscore-prefixed maintenance helpers for the golden PySpark snapshots in
`tests/fixtures/golden_scripts/`. Contributors need these when a template changes;
see [docs/TESTING.md](../docs/TESTING.md) § Golden File Updates for the procedure.

## Deployment

All infrastructure is deployed via CDK. See `deploy-cdk/` for stack definitions.

```bash
cd deploy-cdk
npx cdk deploy --all --require-approval never
```

## Adding New Scripts

When adding utility scripts:

1. Include usage instructions in script header
2. Use color-coded output for visibility
3. Add error handling with `set -e`
4. Document required environment variables
5. Add entry to this README
