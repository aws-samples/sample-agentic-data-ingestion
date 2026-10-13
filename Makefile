# AgenticDataIngestion Test & Quality Makefile
# Usage: make check        (pre-commit: name-check + lint + type-check + security + tests)
#        make test-fast    (unit + contract tests only)
#        make test-ci      (pre-merge, all layers)
#        make test-all     (nightly, all tests)
#
# Tests are run in separate pytest invocations per layer to avoid
# sys.modules cross-contamination between Lambda handler imports.

.PHONY: check name-check lint format format-check type-check security shellcheck prune-cdk-out test-fast test-scope test-meta test-ci test-all test-e2e test-e2e-trigger test-cdk test-frontend test-coverage help

# ── Quality gate (run before every commit) ────────────────────────────
check: name-check lint type-check security test-fast

# ── Naming guard (p31 R7.4 / R7.5) ────────────────────────────────────
# Fails on any new pre-rename project name outside the documented deliberate
# allow-list, and on deletion of a naming fence (the dual snake+kebab Glue ARN
# prefix forms, the PipelineWatermarksTable logical ID, the debugger agent-id
# lockstep). Runs first in `check` because it costs about a second.
#
# --naming-only deliberately skips the scanner's internal-marker half: a few test
# files legitimately pin an account id, which is a publication concern and not a
# naming one. Wiring the full scan here would ship a red gate.
#
# Unlike lint/type-check/security this target is BLOCKING when the scanner is
# present.
#
# THE SCANNER LIVES ABOVE THIS PROJECT, AND SKIPPING IS THE CORRECT BEHAVIOUR
# WITHOUT IT. It sits in internal-docs/scripts/ because it necessarily contains
# every internal marker it searches for, including real AWS account ids, so it must
# not travel with the published project. A published copy of this repository has no
# internal names to check and legitimately cannot run this gate, so the target
# reports and succeeds rather than failing on a missing file. Do not "fix" that by
# hardcoding a path back inside the project.
SCANNER := ../internal-docs/scripts/check-internal-refs.sh

name-check:
	@if [ -x "$(SCANNER)" ]; then \
		"$(SCANNER)" --naming-only; \
	else \
		echo "name-check SKIPPED: internal scanner not present ($(SCANNER))."; \
		echo "  Expected in a published checkout — this gate checks for internal"; \
		echo "  names that a public copy does not contain."; \
	fi

# ── Linting & formatting ─────────────────────────────────────────────
lint:
	uv run ruff check lambda/ shared/ mcp/ tests/ || echo "⚠ Lint issues found (non-blocking — fix incrementally)"

format:
	uv run ruff format lambda/ shared/ mcp/ tests/
	uv run ruff check --fix lambda/ shared/ mcp/ tests/

format-check:
	uv run ruff format --check lambda/ shared/ mcp/ tests/

# ── Type checking ─────────────────────────────────────────────────────
type-check:
	uv run mypy shared/ --no-error-summary || echo "⚠ Type issues in shared/ (non-blocking)"
	MYPYPATH=shared uv run mypy lambda/ --no-error-summary || echo "⚠ Type issues in lambda/ (non-blocking)"
# NOTE: MYPYPATH=shared is set ONLY for the lambda/ check so mypy can resolve the
# Lambda-layer shared imports (env_utils, response_utils, …) and recover their real
# signatures instead of treating them as Any. It is deliberately NOT set on the
# shared/ check above, nor in pyproject.toml [tool.mypy], because putting shared on
# the global mypy path makes shared modules resolve as bare top-level names and
# silently disables the `module = "shared.*"` strict override (dropping shared/ from
# 125 strict errors to 67 unchecked). Keeping it scoped preserves shared/ strictness.

# ── Security scanning ─────────────────────────────────────────────────
security:
	uv run bandit -r lambda/ shared/ mcp/ agentcore/ -c pyproject.toml -q || echo "⚠ Security issues found (non-blocking)"

# ── Shell linting (R1.5) ──────────────────────────────────────────────
# Deliberately NOT a prerequisite of `check`: shellcheck is a system binary
# outside the uv lockfile, so a hard dependency turns the quality gate red on
# any machine that has not installed it. `tests/unit/test_deploy_interpreter_probe.py`
# runs the same command and skips when the binary is absent; this target is the
# explicit, unskippable form.
shellcheck:
	shellcheck deploy.sh destroy.sh scripts/deploy_prereq_lib.sh scripts/destroy_lib.sh scripts/prune_cdk_out.sh scripts/cleanup_agentcore_runtimes.sh

# ── cdk.out housekeeping ──────────────────────────────────────────────
# Standalone form of the prune deploy.sh runs before bootstrap. Removes only
# the asset.* staging dirs the current cdk.out/manifest.json no longer
# references — NOT cdk.out itself, which is the cache that lets unchanged
# container images and Lambda layers be reused instead of rebuilt.
#
# Deliberately NOT a prerequisite of `check`: it mutates the build tree, and a
# quality gate should not delete things as a side effect. Exits 0 even when it
# refuses (no manifest, synth in flight), so it is safe to run at any time.
prune-cdk-out:
	./scripts/prune_cdk_out.sh

# ── Hermetic test environment ─────────────────────────────────────────
# Every test target below except test-e2e / test-e2e-trigger runs with AWS
# credentials removed. A unit test once reached real DynamoDB with the
# developer's own credentials and passed, then failed on a machine without
# them. The default should be hermetic: a test that needs AWS fails here
# instead of quietly using whatever account the shell points at. test-cdk
# gets it too, because its vitest suite synthesizes stacks in-process with no
# lookups. test-frontend is untouched.
NO_AWS_CREDS := env -u AWS_PROFILE -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true

# Pre-commit: unit + contract tests (separate invocations)
#
# MUST RUN ALONE. tests/unit/test_marker_selection_prop.py writes a stub test
# file into all five layer directories for ~53s; a second concurrent pytest
# process that observes that window fails the placement and marker meta-tests.
# See docs/TESTING.md for the tier this belongs to.
test-fast:
	$(NO_AWS_CREDS) uv run python -m pytest tests/unit/ --tb=short -q --override-ini="addopts="
	$(NO_AWS_CREDS) uv run python -m pytest tests/contracts/ --tb=short -q --override-ini="addopts="

# ── Scoped tiers (per-task gates) ─────────────────────────────────────
# One test directory — the per-task instrument. Pick the directory from the
# code-to-test mapping in docs/TESTING.md. Far cheaper than test-fast; re-run
# both rather than trusting any timing written down here.
test-scope:
	@test -n "$(DIR)" || { echo "usage: make test-scope DIR=tests/unit/lambdas/<name>/"; exit 2; }
	$(NO_AWS_CREDS) uv run python -m pytest $(DIR) --tb=short -q --override-ini="addopts="

# The four cross-layer meta-tests. They live at the tests/unit/ ROOT, so a
# scoped directory run never reaches them — run this whenever a test file is
# added, moved or renamed. Also MUST RUN ALONE, for the stub-window reason above.
test-meta:
	$(NO_AWS_CREDS) uv run python -m pytest \
		tests/unit/test_hierarchy_placement_prop.py \
		tests/unit/test_marker_selection_prop.py \
		tests/unit/test_no_duplicate_mocking_prop.py \
		tests/unit/test_migration_preservation_prop.py \
		--tb=short -q --override-ini="addopts="

# Pre-merge: unit + contract + integration with coverage
test-ci:
	$(NO_AWS_CREDS) uv run python -m pytest tests/unit/ --tb=short -q --override-ini="addopts=" --cov=lambda --cov=shared --cov-report=term-missing
	$(NO_AWS_CREDS) uv run python -m pytest tests/contracts/ --tb=short -q --override-ini="addopts="
	$(NO_AWS_CREDS) uv run python -m pytest tests/integration/ --tb=short -q --override-ini="addopts="

# Nightly: all Python tests + CDK
test-all:
	$(NO_AWS_CREDS) uv run python -m pytest tests/unit/ --tb=short -q --override-ini="addopts="
	$(NO_AWS_CREDS) uv run python -m pytest tests/contracts/ --tb=short -q --override-ini="addopts="
	$(NO_AWS_CREDS) uv run python -m pytest tests/integration/ --tb=short -q --override-ini="addopts="
	$(NO_AWS_CREDS) uv run python -m pytest tests/cdk/ --tb=short -q --override-ini="addopts="

# E2E only (requires real AWS credentials)
test-e2e:
	AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true uv run python -m pytest tests/e2e/ -m e2e --tb=short -v --override-ini="addopts="

# E2E with triggering/mutating tests (requires real AWS + opt-in)
test-e2e-trigger:
	AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true AGENTIC_DATA_INGESTION_E2E_TRIGGER=true \
		uv run python -m pytest tests/e2e/ -m e2e --tb=short -v --override-ini="addopts="

# CDK assertion tests (excludes integration tests that need real AWS)
test-cdk:
	cd deploy-cdk && $(NO_AWS_CREDS) npx vitest run --exclude test/integration.test.ts

# Frontend tests
test-frontend:
	cd dashboard-react && npx vitest --run
	cd dashboard-react && npx tsc --noEmit

# Coverage report with minimum threshold
test-coverage:
	$(NO_AWS_CREDS) uv run python -m pytest tests/unit/ tests/contracts/ --override-ini="addopts=" --cov=lambda --cov=shared --cov-report=html --cov-report=term-missing --cov-fail-under=80

help:
	@echo "Available targets:"
	@echo "  check          Pre-commit: name-check + lint + type-check + security + tests"
	@echo "  name-check     Fail on new legacy project names / deleted naming fences (blocking)"
	@echo "  lint           Run ruff linter"
	@echo "  format         Auto-format with ruff"
	@echo "  format-check   Check formatting without changes"
	@echo "  type-check     Run mypy type checker"
	@echo "  security       Run bandit security scanner"
	@echo "  shellcheck     Lint deploy.sh and its helper scripts (needs shellcheck installed)"
	@echo "  prune-cdk-out  Remove orphaned cdk.out asset staging (keeps the asset cache)"
	@echo "  test-fast      Checkpoint gate: unit + contract (9-11 min — must run alone)"
	@echo "  test-scope     Per-task gate: one directory, e.g. make test-scope DIR=tests/unit/lambdas/ws_connect/"
	@echo "  test-meta      The four cross-layer meta-tests (run when a test file moves; must run alone)"
	@echo "  test-ci        Pre-merge: unit + contract + integration"
	@echo "  test-all       Nightly: all Python tests + CDK"
	@echo "  test-e2e       E2E tests (requires AWS credentials)"
	@echo "  test-e2e-trigger E2E tests including triggering/mutating tests"
	@echo "  test-cdk       CDK assertion tests (TypeScript/Vitest)"
	@echo "  test-frontend  Frontend tests (Vitest + tsc)"
	@echo "  test-coverage  Coverage report with 80% minimum"
