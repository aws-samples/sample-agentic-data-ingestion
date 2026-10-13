#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# deploy_prereq_lib.sh — pure helpers for deploy.sh's own computations.
#
# Named for the prerequisite checks it started as; it now also carries the pure
# parts of the post-deploy stack completeness check (see the second section
# below). The organising principle is unchanged and is the reason the name has
# not been churned: what lives here is every computation deploy.sh performs that
# touches nothing outside its arguments.
#
# Sourceable and side-effect-free: this file only defines things. Nothing runs
# at source time, nothing reads or writes the caller's check state
# (FAILURES/WARNINGS), and nothing writes to stdout except a function's own
# resolved value. That is what makes the logic exercisable directly:
#
#   bash -c '. scripts/deploy_prereq_lib.sh; resolve_python_interpreter'
#
# ...instead of only through a full prerequisite sweep. Translating a result
# into a user-facing failure message stays in deploy.sh, next to fail().
#
# Usage (from deploy.sh, after SCRIPT_DIR is set):
#   # shellcheck source=scripts/deploy_prereq_lib.sh
#   . "$SCRIPT_DIR/scripts/deploy_prereq_lib.sh"

# Candidate interpreter names, in probe order.
#
# python3 first, so a host whose default interpreter already satisfies the
# requirement is used as-is. python3.12 second, because on Amazon Linux 2023
# `dnf install python3.12` installs /usr/bin/python3.12 and leaves python3 at
# 3.9 — a host that satisfies the actual requirement under a non-default name.
# deploy.sh reads this list when naming candidates in its failure message, so
# adding a candidate here extends both the probe and the message.
ADI_PYTHON_CANDIDATES=(python3 python3.12)

# resolve_python_interpreter — echo the first candidate reporting Python 3.12+.
#
# stdout: the resolved interpreter name (as invocable via PATH), on success.
# return: 0 when a candidate qualifies, 1 when none does (no stdout).
#
# The gate is the interpreter's own exit code, not parsed output: an interpreter
# is the authority on its own version, and a caller cannot then mistake a
# version *string* for a version comparison.
resolve_python_interpreter() {
  local candidate
  for candidate in "${ADI_PYTHON_CANDIDATES[@]}"; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c \
      'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)' \
      >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

# bootstrap_targets — echo the CDK environments to bootstrap, one per line.
#
# $1: AWS account ID.  $2: the app region.
# stdout: one `aws://<account>/<region>` per line, app region first, with no
#         environment repeated. Always at least one line.
# return: 0.
#
#   $ bootstrap_targets 123456789012 us-west-2
#   aws://123456789012/us-west-2
#   aws://123456789012/us-east-1
#   $ bootstrap_targets 123456789012 us-east-1
#   aws://123456789012/us-east-1
#
# us-east-1 is in the list whenever the app region differs from it, and that is
# not optional: the EdgeWaf stack's CLOUDFRONT-scope WAF can only live in
# us-east-1, and the Hosting stack consumes its ARN through a cross-region
# reference. Dropping it leaves a deploy that fails part-way through Hosting.
#
# The deduplication is the point. Naming the same environment twice in one
# `cdk bootstrap` invocation makes CDK open two concurrent change sets against
# the same bootstrap stack, which fails with
# `AlreadyExistsException: ChangeSet ... ClientToken mismatch` — so an app
# region of us-east-1 used to block its own deploy on a self-inflicted
# collision.
#
# App region first, deliberately: it preserves the argument order `deploy.sh`
# has always passed, so the app region is still the environment whose failure
# surfaces first.
#
# With two candidate environments, "deduplicate" is one comparison. If a third
# fixed region is ever added here, this needs a real dedup pass rather than
# another `[ ... ] ||` line.
bootstrap_targets() {
  local account="$1" region="$2"
  printf '%s\n' "aws://$account/$region"
  [ "$region" = "us-east-1" ] || printf '%s\n' "aws://$account/us-east-1"
}
# ── Post-deploy stack completeness ────────────────────────────────────
#
# The completeness check exists because an interrupted `cdk deploy --all` can
# leave sibling stacks undeployed, and the failure is silent: no Orchestration
# stack means pipelines never execute, with nothing saying so. The pure parts of
# that check live here — the stack sets, the physical-name construction and the
# status classification — so they can be asserted directly. The
# `aws cloudformation describe-stacks` calls stay in deploy.sh: the I/O is not
# the part that can be wrong in an interesting way.
#
# Two sets, not one, because the app is deployed to two regions. Every stack
# `cdk deploy --all` creates lands in the app region EXCEPT EdgeWaf, whose
# CLOUDFRONT-scope WebACL can only exist in us-east-1
# (`bin/agentic_data_ingestion.ts` pins `region: 'us-east-1'` on it, and
# nothing else). The split is therefore a fact about the CDK app, not a
# convenience here, and `tests/unit/test_deploy_stack_verification.py` derives
# it from that file rather than from these arrays — a list checked against its
# own transcription cannot notice the producer drifting (`LGR-28b4d75-5`).
#
# do NOT move EdgeWaf into ADI_APP_REGION_STACKS: it would be queried in the app
# region, where it does not exist, so a *correct* deploy would report a missing
# stack and exit 1.

# Logical stack names `cdk deploy --all` creates in the application region.
# Physical names are built by stack_name; see its comment for the form.
ADI_APP_REGION_STACKS=(Foundation Data Auth AgentCore Compute Orchestration Hosting)

# Logical stack names pinned to us-east-1 regardless of the app region.
# An array rather than a single name so a second edge-region stack is one
# entry rather than a second code path; deploy.sh loops over it either way.
ADI_EDGE_REGION_STACKS=(EdgeWaf)

# TestFixturesStack is deliberately in neither list. It is constructed only
# behind the `adi:deployTestFixtures` context gate, so a plain
# `cdk deploy --all` does not create it and expecting it would fail a correct
# deploy. Absence is the normal case, and this check has no way to learn which
# context flags a past deploy carried.

# The status deploy.sh substitutes when describe-stacks cannot find a stack.
# Not a CloudFormation status: a stack that does not exist has no status, and
# the classification needs to tell "absent" apart from "present but unhealthy"
# because only the first has a re-run as its fix.
ADI_STACK_ABSENT=MISSING

# app_region_stacks / edge_region_stacks — echo a stack set, one name per line.
#
# stdout: the logical names, in declaration order.  return: 0.
#
# Accessors rather than callers reading the arrays directly, so the sets are
# reachable from a subshell (`bash -c '. lib; app_region_stacks'`) the same way
# every other value in this file is.
app_region_stacks() {
  local name
  for name in "${ADI_APP_REGION_STACKS[@]}"; do
    printf '%s\n' "$name"
  done
}

edge_region_stacks() {
  local name
  for name in "${ADI_EDGE_REGION_STACKS[@]}"; do
    printf '%s\n' "$name"
  done
}

# stack_name — echo the deployed CloudFormation name of one logical stack.
#
# $1: logical name (e.g. Hosting).  $2: environment name (e.g. dev).
# stdout: `AgenticDataIngestion-<logical>-<env>`.
# return: 0, or 2 when either argument is empty.
#
#   $ stack_name Hosting dev
#   AgenticDataIngestion-Hosting-dev
#
# The environment is an argument, not a default here: it comes from the
# `environment` CDK context key and defaults to dev in deploy.sh, next to the
# rest of that script's operator input handling.
#
# Both arguments are required because the empty forms are silently plausible —
# `AgenticDataIngestion--dev` and `AgenticDataIngestion-Hosting-` are
# well-formed strings naming stacks that do not exist, and describe-stacks
# would report them as missing rather than as a bad call.
stack_name() {
  local logical="${1:-}" env_name="${2:-}"
  [ -n "$logical" ] || return 2
  [ -n "$env_name" ] || return 2
  printf '%s\n' "AgenticDataIngestion-${logical}-${env_name}"
}

# classify_stack_status — reduce a stack status to what the caller must do.
#
# $1: a CloudFormation stack status, or $ADI_STACK_ABSENT for a stack that
#     describe-stacks could not find.
# stdout: `ok`, `missing`, or `warn` — exactly one, always one.
# return: 0.
#
# Three healthy states, and only three: a stack finished creating, finished
# updating, or finished rolling an update back. The last is healthy because the
# rollback completed — the stack is running its previous, working template.
#
# Everything else is `warn`, and `warn` still fails the check. That is the
# load-bearing part: ROLLBACK_COMPLETE (a failed *create*, so the stack is an
# empty shell), UPDATE_ROLLBACK_FAILED, DELETE_IN_PROGRESS and any
# *_IN_PROGRESS are not working deployments, and a check that passed them would
# report a half-deployed system as complete. So the default arm is deliberately
# a catch-all rather than an enumeration of known-bad states: a status this
# function has never heard of must not read as healthy.
#
# `missing` is separated from `warn` only because the two have different fixes —
# an absent stack is fixed by re-running deploy.sh, an unhealthy one is not.
# Both are failures.
classify_stack_status() {
  case "$1" in
    CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE) printf 'ok\n' ;;
    "$ADI_STACK_ABSENT") printf 'missing\n' ;;
    *) printf 'warn\n' ;;
  esac
}
