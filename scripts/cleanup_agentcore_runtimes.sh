#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# cleanup_agentcore_runtimes.sh — clear the AgentCore RUNTIMES a failed teardown
# abandoned (p64 R2.5, R2.6).
#
# A failed `destroy.sh` can leave AgentCore runtimes live in an account with no
# stack managing them: the `--retain-resources` retry deletes the stack while
# ABANDONING the resources that would not go. Their physical names are hardcoded
# with no environment suffix, so they are still occupied and the next deploy
# collides with them. This tool lists those runtimes and, only when explicitly
# asked, deletes them.
#
# RUNTIMES, and the name says so on purpose. `list-agent-runtimes` cannot see
# `AWS::BedrockAgentCore::Memory`, which is a different resource type on a
# different API (`bedrock-agentcore-control list-memories`). This system has one,
# declared at `deploy-cdk/lib/agentcore-stack.ts:438`, and it is the ONE AgentCore
# resource whose name does carry `${environment}`. A sweep over runtimes returns
# it never, so calling this "AgentCore cleanup" would overstate what it covers.
# Delete a leftover Memory by hand; this tool will not report it and will not
# touch it.
#
# The API is `bedrock-agentcore-control`, not `bedrock-agent` (R2.5). All of this
# system's runtimes are `AWS::BedrockAgentCore::Runtime`, the runtime role trusts
# `bedrock-agentcore.amazonaws.com`, and no `AWS::Bedrock::Agent` exists anywhere
# in `deploy-cdk/` — so `aws bedrock-agent delete-agent` targets a different
# service and cannot find these resources at all.
#
# Usage:
#   scripts/cleanup_agentcore_runtimes.sh --profile <name> --region <name>
#                [--environment <name>] [--prefix <name>] [--delete]
#
#   --profile <name>      REQUIRED. The profile whose account is swept. The
#                         account is derived from it, never written down here.
#   --region <name>       REQUIRED. The region holding the runtimes.
#   --environment <name>  Environment suffix in the AgentCore stack name
#                         (default: dev). Used ONLY to look that stack up — the
#                         runtime names carry no environment suffix, which is
#                         the whole reason they collide.
#   --prefix <name>       Runtime-name prefix to select, matched literally and
#                         anchored at the start of the name (default:
#                         agentic_data_ingestion_, which selects every runtime
#                         this system declares — agent and MCP alike). Pass
#                         agentic_data_ingestion_agent_ for the agent runtimes
#                         only. The count differs and the report says which
#                         set you got.
#   --delete              Actually delete. Without it NOTHING is deleted.
#   -h, --help            Print this block and exit. Listed here because it is
#                         accepted, and a flag the parser takes but the usage
#                         omits is undiscoverable.
#
# Without --delete this enumerates what it would delete and stops, having called
# nothing but read-only APIs (R2.6). Even WITH --delete it refuses while the
# AgentCore CloudFormation stack still exists, because a runtime that stack still
# claims is a live resource rather than an orphan — see
# `compose_agentcore_sweep_refusal` in scripts/destroy_lib.sh for that argument
# and for why this tool is not wired into destroy.sh.
#
# There is no interactive confirmation prompt, for the reason destroy.sh gives
# (design decision W1-D3): non-interactive shells are the norm for this repo's
# tooling and a prompt cannot be exercised against a hermetic test PATH, so the
# gate is a flag.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The pure computations live in the sourceable, side-effect-free teardown library
# so they can be exercised directly instead of only through a run that deletes
# real AI agent runtimes (LGR-b5b76ed-1). This script is the thin driver: it
# turns each resolved value into an API call and each failure into an
# operator-facing message.
#
# Sharing that library is not the same as being wired into destroy.sh. Nothing in
# destroy.sh calls either of the two functions this file uses from it.
# shellcheck source=scripts/destroy_lib.sh
. "$SCRIPT_DIR/destroy_lib.sh"

# ── Arguments ─────────────────────────────────────────────────────────
#
# No AWS account identifier appears anywhere in this file, and no region or
# profile has a default. LGR-1166391-2 records a hardcoded account being copied
# verbatim into an operation aimed at the OTHER of this system's two accounts.
# This tool DELETES, so there is no literal here to copy, no ambient AWS_REGION
# fallback and no profile default: the operator names the profile, the account is
# derived from it by resolve_account, and both are echoed before anything is
# touched.
PROFILE=""
REGION=""
ENVIRONMENT=""
PREFIX=""
# R2.6. Zero is the required default and stays the initial value: a run without
# --delete must reach no mutating call at all, which is the property that makes
# this tool safe to land on a system whose runtimes are shared state. Defaulting
# this to 1 would make the safe mode the one you have to remember to ask for.
DELETE=0
# The prefix that selects EVERY runtime this system declares — 14 of them, 5 MCP
# server runtimes plus 9 agent runtimes, per docs/ARCHITECTURE.md § System
# Inventory. All 14 names are hardcoded with no `${environment}` suffix
# (agentcore-stack.ts:65-69 for the MCP five, :518 :536 :554 :572 :599 :624 :652
# :677 :702 for the agent nine), so all 14 collide on the next deploy and all 14
# are what this tool exists to clear. A default of
# `agentic_data_ingestion_agent_` would cover 9 and leave the other 5 occupying
# their names — the tool failing its own purpose, quietly, for a third of its
# targets.
#
# The narrower prefix is a supported argument rather than the default because
# choosing it is a real choice an operator might want, not because the wide one
# is unsafe here. `select_agentcore_runtimes` carries the full argument,
# including what makes this different from the Glue sweep's refusal to use a
# prefix at all, and the stack-existence refusal below is the guard that does not
# rest on names.
DEFAULT_RUNTIME_PREFIX="agentic_data_ingestion_"
# Whether --prefix was PASSED, tracked separately from its value, because "absent"
# and "passed empty" must not collapse. The obvious `${PREFIX:-$DEFAULT}` collapses
# them — `:-` substitutes on empty as well as on unset — so an explicit
# `--prefix ''` would be silently upgraded to the widest prefix this tool has and
# would select every runtime the system declares on behalf of a caller who asked
# for nothing. That is over-selection on a delete, so it gets a flag of its own
# rather than a default-substitution idiom.
PREFIX_GIVEN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --profile)
      shift
      [ $# -gt 0 ] || { echo "error: --profile requires a value" >&2; exit 2; }
      PROFILE="$1"
      ;;
    --region)
      shift
      [ $# -gt 0 ] || { echo "error: --region requires a value" >&2; exit 2; }
      REGION="$1"
      ;;
    --environment)
      shift
      [ $# -gt 0 ] || { echo "error: --environment requires a value" >&2; exit 2; }
      ENVIRONMENT="$1"
      ;;
    --prefix)
      shift
      [ $# -gt 0 ] || { echo "error: --prefix requires a value" >&2; exit 2; }
      PREFIX="$1"
      PREFIX_GIVEN=1
      ;;
    --delete) DELETE=1 ;;
    -h|--help)
      # Print the header block: skip the shebang, then emit comment lines with
      # their marker stripped and stop at the first line that is not a comment.
      # Self-delimiting on purpose — the same remedy LGR-4ea43f3-5 applied to
      # deploy.sh and destroy.sh, because any hardcoded end line number goes
      # stale silently the first time a header line is added or removed.
      awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) echo "error: unknown argument '$1' (see --help)" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$PROFILE" ] || { echo "error: --profile is required (see --help)" >&2; exit 2; }
[ -n "$REGION" ] || { echo "error: --region is required (see --help)" >&2; exit 2; }
# `dev` matches the default of the `environment` CDK context key at
# deploy-cdk/bin/agentic_data_ingestion.ts:119, so an operator who deployed
# without --environment does not have to pass one here either.
ENV_NAME="${ENVIRONMENT:-dev}"
if [ "$PREFIX_GIVEN" -eq 1 ]; then
  RUNTIME_PREFIX="$PREFIX"
else
  RUNTIME_PREFIX="$DEFAULT_RUNTIME_PREFIX"
fi
# An explicit `--prefix ''` reaches here as empty and is refused rather than
# defaulted, which is only possible because PREFIX_GIVEN kept the two apart above.
# `select_agentcore_runtimes` returns 2 on an empty prefix for the same reason;
# this check is what turns that into a message instead of a silently empty
# enumeration reading as "nothing to sweep" — and it runs before the first AWS
# call, so a refused prefix touches nothing.
[ -n "$RUNTIME_PREFIX" ] || { echo "error: --prefix cannot be empty (see --help)" >&2; exit 2; }
AGENTCORE_STACK="AgenticDataIngestion-AgentCore-${ENV_NAME}"

# ── Reporting ─────────────────────────────────────────────────────────
say()  { printf '%s\n' "$*"; }
item() { printf '    %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# ── Target, echoed before anything is read ────────────────────────────
ACCOUNT=""
ACCOUNT="$(resolve_account "$PROFILE")" \
  || die "could not resolve the account for profile '$PROFILE'. Nothing was read and nothing was deleted."
say "==> Target: account $ACCOUNT, region $REGION, profile $PROFILE"
say "==> Selecting AgentCore runtimes whose name starts with '$RUNTIME_PREFIX'"
if [ "$DELETE" -eq 1 ]; then
  say "    MODE: --delete — the runtimes listed below WILL be deleted."
else
  say "    MODE: dry run — nothing will be deleted (pass --delete to delete)."
fi
say "    NOT covered: AWS::BedrockAgentCore::Memory. Different resource type,"
say "    different API (list-memories), invisible to this tool. Check it by hand."
say ""

# ── Is the stack still standing? ──────────────────────────────────────
#
# Read-only, and it runs before the enumeration so the report can say up front
# whether a delete is even permitted. A non-zero return means the stack is not
# there, which is the state this tool is FOR: the runtimes below have nothing
# managing them.
STACK_STATUS=""
STACK_STILL_STANDING=0
if STACK_STATUS="$(aws cloudformation describe-stacks \
      --stack-name "$AGENTCORE_STACK" \
      --region "$REGION" \
      --profile "$PROFILE" \
      --query 'Stacks[0].StackStatus' \
      --output text 2>/dev/null)"; then
  STACK_STILL_STANDING=1
fi

# ── Enumerate ─────────────────────────────────────────────────────────
#
# One two-field text projection, `agentRuntimeName<TAB>agentRuntimeId`. The id is
# read from the API here and nowhere else; see the section header above
# `select_agentcore_runtimes` for why it is never composed from the name.
#
# A listing that came back truncated can only make this tool select FEWER
# runtimes than exist — it can miss an orphan, never delete something extra — so
# truncation is the safe direction to be wrong in and is not treated as a
# failure. A listing that came back as an ERROR is different and is fatal: an
# enumeration that did not happen cannot support a claim about what is there.
#
# The CLI's stderr is deliberately NOT captured or suppressed: it goes straight
# to the operator's terminal, so an access-denied or a bad region reads as itself
# rather than as this script's paraphrase of it. Only stdout is captured, because
# only stdout is the data.
RUNTIME_ROWS=""
if ! RUNTIME_ROWS="$(aws bedrock-agentcore-control list-agent-runtimes \
      --region "$REGION" \
      --profile "$PROFILE" \
      --query 'agentRuntimes[].[agentRuntimeName,agentRuntimeId]' \
      --output text)"; then
  die "could not list AgentCore runtimes in $REGION (the CLI's own error is above). Nothing was deleted."
fi

SELECTED_NAMES=()
SELECTED_IDS=()
while IFS= read -r row; do
  [ -n "$row" ] || continue
  SELECTED_NAMES+=("${row%%$'\t'*}")
  SELECTED_IDS+=("${row#*$'\t'}")
done < <(printf '%s\n' "$RUNTIME_ROWS" | select_agentcore_runtimes "$RUNTIME_PREFIX")

if [ "${#SELECTED_IDS[@]}" -eq 0 ]; then
  say "No AgentCore runtime in account $ACCOUNT / region $REGION matches '$RUNTIME_PREFIX'."
  say "Nothing was deleted."
  exit 0
fi

say "==> ${#SELECTED_IDS[@]} runtime(s) selected. The id is what a delete uses, so read it:"
i=0
while [ "$i" -lt "${#SELECTED_IDS[@]}" ]; do
  item "${SELECTED_NAMES[$i]}"
  item "    id ${SELECTED_IDS[$i]}"
  i=$(( i + 1 ))
done
say ""

# ── The gate the stack status decides ─────────────────────────────────
if [ "$STACK_STILL_STANDING" -eq 1 ]; then
  compose_agentcore_sweep_refusal "$AGENTCORE_STACK" "$REGION" "$STACK_STATUS" \
    || die "internal: could not compose the refusal notice for $AGENTCORE_STACK"
  say ""
  # A dry run that was never going to delete exits 0: it reported the state
  # correctly and the operator asked for nothing. A --delete run exits non-zero,
  # because the operator asked for a delete and did not get one, and a zero exit
  # there would let a script read the refusal as a completed sweep.
  if [ "$DELETE" -eq 1 ]; then
    say "Nothing was deleted."
    exit 1
  fi
  say "Dry run: nothing was deleted, and --delete would have been refused too."
  exit 0
fi
item "stack $AGENTCORE_STACK is absent from $REGION — these runtimes have nothing managing them."
say ""

if [ "$DELETE" -eq 0 ]; then
  say "Dry run: nothing was deleted. Re-run with --delete to delete the ${#SELECTED_IDS[@]} runtime(s) above."
  exit 0
fi

# ── Delete ────────────────────────────────────────────────────────────
#
# `--agent-runtime-id`, with the id exactly as the API returned it. One request
# per runtime, and a failure is recorded and reported rather than fatal: this is
# a cleanup of resources that are already unmanaged, so stopping at the first
# stubborn one would leave the rest of the collision in place for no gain.
FAILED=()
DELETED=0
i=0
while [ "$i" -lt "${#SELECTED_IDS[@]}" ]; do
  name="${SELECTED_NAMES[$i]}"
  id="${SELECTED_IDS[$i]}"
  i=$(( i + 1 ))
  item "deleting $name (id $id)"
  delete_err=""
  if ! delete_err="$(aws bedrock-agentcore-control delete-agent-runtime \
        --agent-runtime-id "$id" \
        --region "$REGION" \
        --profile "$PROFILE" 2>&1 >/dev/null)"; then
    # Printed whole, never `${delete_err%%$'\n'*}` — R2.4's posture one tool over:
    # an unparsed line is a formatting problem, a dropped line is a lost cause.
    FAILED+=("$name")
    item "    FAILED: $delete_err"
    continue
  fi
  DELETED=$(( DELETED + 1 ))
done

say ""
say "Deleted: $DELETED runtime(s)."
if [ "${#FAILED[@]}" -gt 0 ]; then
  say "FAILED to delete ${#FAILED[@]} runtime(s) — still live, still occupying their names:"
  for failed_name in "${FAILED[@]}"; do
    item "$failed_name"
  done
  exit 1
fi
say "Remember the Memory resource this tool does not cover (see --help)."
exit 0
