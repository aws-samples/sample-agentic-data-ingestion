#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# destroy.sh — one-command teardown for Agentic Data Ingestion.
#
# The inverse of ./deploy.sh: removes this system's deployed resources from one
# account and region pair, so a redeploy into an account that already holds a
# previous version does not cost the ~3h manual cleanup it costs today.
#
# Three classes of resource, deleted in this order:
#   1. CloudFormation stacks, in the reverse of the deployment's creation order.
#   2. DynamoDB tables carrying the project prefix. 13 of the 17 tables in
#      data-stack.ts inherit removalPolicy RETAIN, so they SURVIVE stack
#      deletion and their fixed physical names collide on the next deploy.
#   3. IAM roles and customer-managed policies carrying the project prefix.
#      These are account-global: they outlive any one region's stacks.
#
# Usage:
#   ./destroy.sh --profile <name> --region <name> [--environment <name>] [--force]
#                [--continue-past-failed-stacks]
#
#   --profile <name>      REQUIRED. The profile whose account is torn down. The
#                         account is derived from it, never written down here.
#   --region <name>       REQUIRED. The region holding the stacks and tables.
#   --environment <name>  Environment suffix in the stack names (default: dev).
#   --force               Actually delete. Without it NOTHING is deleted.
#   --continue-past-failed-stacks
#                         Opt in to continuing after a stack fails to delete.
#                         The DEFAULT is to stop at the first such stack, and
#                         that default does not change. A skipped stack is
#                         STILL STANDING, and the stacks after it are deleted
#                         in dependency order, so their deletions may fail
#                         BECAUSE of it: expect a cascade and read it as one
#                         cause, not many. Every skipped stack is named on
#                         exit and a run that skipped any exits NON-ZERO.
#                         Scoped to stacks only — it does not widen the table
#                         or IAM steps, which already continue past an absence
#                         and still stop on anything else.
#   -h, --help            Print this block and exit. Listed here because it is
#                         accepted, and a flag the parser takes but the usage
#                         omits is undiscoverable.
#
# Without --force this enumerates every resource selected for deletion and
# stops, having deleted nothing (R1.4). --force deletes them (R1.5). There is
# no interactive confirmation prompt: non-interactive shells are the norm for
# this repo's tooling and a prompt cannot be exercised against a hermetic test
# PATH, so the gate is a flag (design decision W1-D3).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The pure teardown computations live in a sourceable, side-effect-free library
# so they can be exercised directly instead of only through a run that deletes
# real resources (LGR-b5b76ed-1). This script is the thin driver: it turns each
# resolved value into an API call and each failure into an operator-facing
# message. Read scripts/destroy_lib.sh for the contracts — every function there
# writes only its own value to stdout, and every failure is a non-zero return
# with empty stdout, so the checks below test return codes and never output
# emptiness.
# shellcheck source=scripts/destroy_lib.sh
. "$SCRIPT_DIR/scripts/destroy_lib.sh"

# ── Arguments ─────────────────────────────────────────────────────────
#
# No AWS account identifier appears anywhere in this file, and no region or
# profile has a default. LGR-1166391-2 records that a hardcoded account was
# copied verbatim into a deploy aimed at the OTHER of this system's two
# accounts and updated a Data stack in the wrong one. This tool DESTROYS rather
# than updates, so there is no literal here to copy, no ambient AWS_REGION
# fallback, and no profile default: the operator names the profile, the account
# is derived from it by resolve_account, and both are echoed before anything is
# touched (R1.3).
PROFILE=""
REGION=""
ENVIRONMENT=""
FORCE=0
# p64 R2.2. Zero is the ratified posture and stays the initial value: the flag is
# opt-in, so the absent-flag run must be byte-for-byte the run this script
# performed before the flag existed. Defaulting this to 1 would turn the safety
# property the stop posture exists to hold into a footgun on a tool whose every
# action is irreversible, which is why R2.2 states the default as a requirement
# rather than leaving it to taste.
CONTINUE_PAST_FAILED_STACKS=0

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
    --force) FORCE=1 ;;
    --continue-past-failed-stacks) CONTINUE_PAST_FAILED_STACKS=1 ;;
    -h|--help)
      # Print the header block: skip the shebang, then emit comment lines with
      # their marker stripped and stop at the first line that is not a comment.
      # Self-delimiting on purpose. The previous form was `sed -n '2,29p'`, a
      # hardcoded range over this file's own header — correct at the moment it
      # was written and wrong the moment a header line is added or removed,
      # which truncates the usage text or spills `set -euo pipefail` into it as
      # if it were documentation. This is the same remedy LGR-4ea43f3-5 applied
      # to deploy.sh for the identical defect, and the same reason: any end
      # line number here goes stale silently, because --help is asserted almost
      # nowhere. The one assertion there is now lives in
      # tests/unit/test_destroy_lib.py.
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
# without --environment does not have to pass one here either. Composing the
# deployed stack name is this script's job and not the library's, because the
# environment is an operator input the library has no business defaulting.
ENV_NAME="${ENVIRONMENT:-dev}"

# The two spellings of the project prefix. Both are applied because both are in
# use: DynamoDB tables are named `agentic_data_ingestion_*` (data-stack.ts) and
# the CDK `projectName` context default is `agentic-data-ingestion`
# (bin/agentic_data_ingestion.ts:120), which reaches IAM role and policy names.
# They cannot both match one name — they differ at the 8th character — so
# concatenating the two selections needs no de-duplication.
PREFIXES=(agentic_data_ingestion agentic-data-ingestion)

# ── Reporting ─────────────────────────────────────────────────────────
say()  { printf '%s\n' "$*"; }
item() { printf '    %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# Absences are recorded rather than reported as failures (R2.3, R3.4), and
# printed again in the closing summary so a run that skipped nine of seventeen
# tables says so instead of looking like a clean sweep.
ABSENT=()
record_absence() {
  ABSENT+=("$1")
  item "absent  $1 (recorded, continuing)"
}

# Resources ABANDONED by a --retain-resources retry (p64 R2.1), reprinted in the
# closing summary for the same reason ABSENT is — and it is the complement of
# ABSENT, not another flavour of it. An absent resource is gone and costs
# nothing; a retained one is still live, still billed, and no longer managed by
# any template, so it is the one thing a "Teardown complete" line must not
# silently cover. There is no record_retained() helper because the only writer
# is the retry arm, which already prints its own multi-line report at the point
# it happens.
RETAINED=()

# Stacks the operator's --continue-past-failed-stacks flag carried this run past
# (p64 R2.2), reprinted in the closing summary for the same reason ABSENT and
# RETAINED are — and it is a third distinct fact, not a shading of either. An
# ABSENT resource is gone. A RETAINED one is live but its stack is gone. A
# SKIPPED stack is entirely still there, with everything in it, and the run
# walked past it on purpose. That last one is the only one of the three that can
# make the deletions AFTER it fail, which is why the summary names it and why the
# run exits non-zero when this array is non-empty.
SKIPPED_STACKS=()

# skip_failed_stack — record and report one stack this run is continuing past.
#
# Called only from the three arms below that would otherwise `die`, and only when
# CONTINUE_PAST_FAILED_STACKS is 1. The caller does its own `continue`: a
# function cannot `continue` its caller's loop, and having the `die` stay
# literally at each arm keeps the default posture readable at the point it
# applies rather than hidden behind a helper that decides it.
#
# The composition is the library's (LGR-b5b76ed-1) — this is only the API-free
# recording. A composition failure is `die`, not a swallowed error: the notice is
# the operator's only warning that a cascade is coming, and continuing past a
# standing stack WITHOUT printing why would be strictly worse than stopping.
skip_failed_stack() {
  compose_skipped_stack_notice "$1" "$2" "$3" \
    || die "internal: could not compose the skip notice for $1"
  SKIPPED_STACKS+=("$1 ($2)")
  say ""
}

# is_absence_error — does this CLI stderr say the resource is already gone?
#
# The ONLY error class R2.3 and R3.4 license continuing past. Their scope is
# absence at the point of deletion — four of the seventeen tables set
# removalPolicy DESTROY explicitly and are already gone with the stack, and a
# partially-completed earlier teardown leaves arbitrary gaps, so an absence is
# the EXPECTED outcome here rather than a fault.
#
# Everything else falls to the caller's hard-failure arm, deliberately: a
# permission denial or a throttle is not an absence, and treating one as a
# recorded non-event would report a clean teardown over resources that are
# still standing. That is the distinction the design's two-postures section
# draws, and it is the whole reason this is a match on named error codes rather
# than on "the call failed".
is_absence_error() {
  case "$1" in
    *NoSuchEntity*|*ResourceNotFoundException*|*NotFoundException*) return 0 ;;
    *"does not exist"*|*"cannot be found"*|*"not found"*) return 0 ;;
  esac
  return 1
}

# ── Resolve the target account (R1.1, R1.2) ───────────────────────────
#
# First AWS call of the run and read-only (sts:GetCallerIdentity). It runs
# before anything is reported and before anything is touched, which is what
# makes R1.2 enforceable: an unresolvable account terminates the run with
# nothing deleted. resolve_account validates the result as exactly 12 digits,
# because this value is about to be reported to the operator as the account
# being destroyed and a value that merely looks like an account would make that
# report a lie at the one moment the operator is relying on it.
say "==> Resolving target account from profile '$PROFILE'"
ACCOUNT=""
ACCOUNT="$(resolve_account "$PROFILE")" || die "could not resolve the AWS account for profile '$PROFILE' — check the profile exists and its credentials are valid (aws sts get-caller-identity --profile '$PROFILE'). Nothing has been deleted."

# R1.3: the resolved account and the target region, before any deletion.
say "==> Target: account $ACCOUNT, region $REGION, environment $ENV_NAME"
# R3.5. Stated here, next to the region, because this is the moment an operator
# forms the belief that the region scopes the run — and for IAM it does not. A
# us-west-2 teardown removes IAM that a us-east-1 deploy created, and leaving
# that implicit is how an operator gets surprised in the direction that costs
# them another account's roles.
say "    NOTE: IAM roles and policies are account-global, NOT region-scoped."
say "          The region above scopes the stacks and tables only; the IAM step"
say "          below deletes prefix-matching IAM in account $ACCOUNT entirely."
if [ "$FORCE" -eq 1 ]; then
  say "    MODE: --force — the resources enumerated below WILL be deleted."
else
  say "    MODE: enumerate only — nothing will be deleted (pass --force to delete)."
fi
# R2.2's "must announce what it is skipping past", announced HERE as well as at
# each skip. By the time the first stack fails the operator is reading a wall of
# per-stack output, and a mode this consequential should be visible from the
# header of the transcript rather than only from the failure that exercises it.
# Reported whether or not --force was passed: an enumerate-only run is the run an
# operator makes to check what they are about to do, so it is exactly where the
# posture should be legible.
if [ "$CONTINUE_PAST_FAILED_STACKS" -eq 1 ]; then
  say "    MODE: --continue-past-failed-stacks — a stack that fails to delete is"
  say "          SKIPPED rather than fatal. It stays STANDING, so later stacks may"
  say "          fail because of it; skipped stacks are named on exit and this run"
  say "          then exits non-zero."
fi

# ── Enumerate stacks (R1.6, R1.7) ─────────────────────────────────────
#
# The order comes from stack_deletion_order rather than a list transcribed here,
# so that adding a stack to bin/agentic_data_ingestion.ts cannot leave this
# script silently one stack short (design decision W1-D2). The library speaks
# logical names; composing the deployed name is this driver's job.
#
# Termination protection is read during enumeration, in the same describe call
# that establishes existence, so R1.8 needs no second round-trip later.
say "==> Enumerating CloudFormation stacks"
STACK_LOGICAL=()
STACK_NAME=()
STACK_REGION=()
STACK_PROTECTED=()
while IFS= read -r logical; do
  region=""
  region="$(stack_region "$logical" "$REGION")" \
    || die "internal: stack_region rejected '$logical' / '$REGION'"
  physical="AgenticDataIngestion-${logical}-${ENV_NAME}"
  protection=""
  # A non-zero return here means the stack is not there, which is a normal
  # outcome: TestFixtures is context-gated and may never have been deployed,
  # and an earlier partial teardown leaves arbitrary gaps. An absent stack is
  # simply not selected, so it never reaches the deletion loop and cannot make
  # R1.9 fire on a stack that was never there.
  if ! protection="$(aws cloudformation describe-stacks \
        --stack-name "$physical" \
        --region "$region" \
        --profile "$PROFILE" \
        --query 'Stacks[0].EnableTerminationProtection' \
        --output text 2>/dev/null)"; then
    continue
  fi
  STACK_LOGICAL+=("$logical")
  STACK_NAME+=("$physical")
  STACK_REGION+=("$region")
  STACK_PROTECTED+=("$protection")
  if [ "$protection" = "True" ]; then
    item "stack   $physical ($region) [termination protection ON]"
  else
    item "stack   $physical ($region)"
  fi
done < <(stack_deletion_order)

# ── Enumerate prefixed DynamoDB tables (R2.1, R2.4) ───────────────────
#
# Selected by prefix rather than from an enumerated list of the 17 tables, so
# the tool does not need to know which are already absent. select_prefixed's
# EXCLUSIONS are the safety property here: this account may hold resources this
# system did not create, and everything selected is a deletion candidate with
# no undo (R2.4).
say "==> Enumerating DynamoDB tables with the project prefix in $REGION"
TABLES=()
table_names=""
table_names="$(aws dynamodb list-tables \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query 'TableNames[]' \
  --output text 2>/dev/null)" \
  || die "could not list DynamoDB tables in $REGION — check the profile's permissions. Nothing has been deleted."
for prefix in "${PREFIXES[@]}"; do
  while IFS= read -r name; do
    TABLES+=("$name")
    item "table   $name"
  done < <(printf '%s\n' "$table_names" | tr '\t' '\n' | select_prefixed "$prefix")
done

# ── Enumerate prefixed IAM roles and policies (R3.1) ──────────────────
#
# --region is passed for consistency with the calls above and is inert here:
# IAM has a single global endpoint. That is exactly what the R3.5 note above
# warns about, and it is why this step is not inside the region loop.
say "==> Enumerating IAM roles and customer-managed policies (account-global)"
ROLES=()
role_names=""
role_names="$(aws iam list-roles \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query 'Roles[].RoleName' \
  --output text 2>/dev/null)" \
  || die "could not list IAM roles — check the profile's permissions. Nothing has been deleted."
for prefix in "${PREFIXES[@]}"; do
  while IFS= read -r name; do
    ROLES+=("$name")
    item "role    $name"
  done < <(printf '%s\n' "$role_names" | tr '\t' '\n' | select_prefixed "$prefix")
done

# Policies are selected by NAME but deleted by ARN, so each line carries both,
# name first. select_prefixed tests a literal prefix at the start of the line,
# so putting the name first makes the ARN a passenger the filter never reads —
# which matters because every ARN contains the account id and a prefix test
# against an ARN would be a test against the wrong field.
POLICY_ARNS=()
policy_lines=""
policy_lines="$(aws iam list-policies \
  --scope Local \
  --region "$REGION" \
  --profile "$PROFILE" \
  --query 'Policies[].[PolicyName,Arn]' \
  --output text 2>/dev/null)" \
  || die "could not list IAM policies — check the profile's permissions. Nothing has been deleted."
for prefix in "${PREFIXES[@]}"; do
  while IFS= read -r line; do
    POLICY_ARNS+=("${line#*$'\t'}")
    item "policy  ${line%%$'\t'*}"
  done < <(printf '%s\n' "$policy_lines" | select_prefixed "$prefix")
done

SELECTED=$(( ${#STACK_NAME[@]} + ${#TABLES[@]} + ${#ROLES[@]} + ${#POLICY_ARNS[@]} ))
say "==> Selected $SELECTED resource(s): ${#STACK_NAME[@]} stack(s), ${#TABLES[@]} table(s), ${#ROLES[@]} role(s), ${#POLICY_ARNS[@]} policy/policies"

# ── R1.10: an account holding nothing is a success, not an error ───────
#
# Reported before the --force gate rather than after it, so that "there is
# nothing here" reads the same whether or not the operator passed --force. An
# empty selection is also select_prefixed's normal successful outcome, not a
# failure it could have signalled.
if [ "$SELECTED" -eq 0 ]; then
  say "No resource carrying the project prefix was selected in account $ACCOUNT / region $REGION — nothing to delete."
  exit 0
fi

# ── R1.4: the --force gate ────────────────────────────────────────────
#
# Everything above is read-only: describe-stacks, list-tables, list-roles,
# list-policies and one sts call. Every mutating call in this script is below
# this line, which is what makes the gate auditable — a test can assert on a
# stub's argv log that no delete-stack, delete-table, delete-role,
# delete-policy, detach-role-policy, delete-role-policy or
# update-termination-protection was issued without --force.
if [ "$FORCE" -ne 1 ]; then
  say ""
  say "Enumeration only: nothing was deleted. Re-run with --force to delete the $SELECTED resource(s) above."
  exit 0
fi

# ── Delete the stacks (R1.5, R1.6, R1.7, R1.8, R1.9) ──────────────────
#
# Failure posture: STOP on the first failure. Stack deletions are ordered and
# dependent — the order is the reverse of a topological creation order — so
# after a failed deletion the next request either fails on a live dependency or
# removes something a still-standing stack needs. Continuing is unsafe in a way
# that continuing past a missing table is not, which is why this loop exits and
# the table and IAM loops below do not.
say ""
say "==> Deleting CloudFormation stacks (${#STACK_NAME[@]}), most-dependent first"
i=0
while [ "$i" -lt "${#STACK_NAME[@]}" ]; do
  physical="${STACK_NAME[$i]}"
  region="${STACK_REGION[$i]}"
  protection="${STACK_PROTECTED[$i]}"
  i=$(( i + 1 ))

  # R1.8. The Data stack is the one that carries terminationProtection: true
  # (deploy-cdk/lib/data-stack.ts:33 — both spec documents say :33 and that is
  # what is in the tree at this commit). It is identified by what
  # describe-stacks reported during enumeration, NOT by matching the name
  # `Data`: protection is a property of the deployed stack, an operator can
  # have enabled it by hand on any stack, and a stack whose protection this
  # tool did not disable fails its delete with an error that reads nothing like
  # "protected". Reading the deployed state means a hand-protected Hosting
  # stack is handled by the same code path.
  if [ "$protection" = "True" ]; then
    item "disabling termination protection on $physical"
    protect_err=""
    if ! protect_err="$(aws cloudformation update-termination-protection \
          --stack-name "$physical" \
          --region "$region" \
          --profile "$PROFILE" \
          --no-enable-termination-protection 2>&1 >/dev/null)"; then
      say ""
      # R2.2's opt-in arm. This stack's delete was never even requested, so it
      # is as standing as a stack can be — which is precisely a skip and not a
      # partial success. The full stderr goes to the notice; the die below keeps
      # its first-line form, because a message that terminates the run is read
      # as a sentence and the reason block is what the notice exists to carry.
      if [ "$CONTINUE_PAST_FAILED_STACKS" -eq 1 ]; then
        skip_failed_stack "$physical" "$region" \
          "termination protection could not be disabled, so the delete was never requested:
$protect_err"
        continue
      fi
      die "could not disable termination protection on $physical ($region): ${protect_err%%$'\n'*}. Stopped — no subsequent stack deletion was requested."
    fi
  fi

  item "deleting $physical ($region)"
  delete_err=""
  if ! delete_err="$(aws cloudformation delete-stack \
        --stack-name "$physical" \
        --region "$region" \
        --profile "$PROFILE" 2>&1 >/dev/null)"; then
    say ""
    # R2.2's opt-in arm. CloudFormation refused the request, so nothing was even
    # attempted against the stack's resources and it is untouched.
    if [ "$CONTINUE_PAST_FAILED_STACKS" -eq 1 ]; then
      skip_failed_stack "$physical" "$region" \
        "CloudFormation refused the delete request:
$delete_err"
      continue
    fi
    die "deletion of stack $physical ($region) failed: ${delete_err%%$'\n'*}. Stopped — no subsequent stack deletion was requested."
  fi

  # Waiting is not politeness, it is the ordering. delete-stack returns as soon
  # as CloudFormation accepts the request, so without the wait the next stack's
  # delete would be requested while this one is still standing — which defeats
  # the whole point of deleting in dependency order.
  if ! aws cloudformation wait stack-delete-complete \
        --stack-name "$physical" \
        --region "$region" \
        --profile "$PROFILE" >/dev/null 2>&1; then
    # R1.9 wants the reported reason, and the waiter does not carry one, so ask
    # CloudFormation — BOTH questions, every time (p64 R2.3).
    #
    # The previous shape asked for StackStatusReason and consulted
    # describe-stack-events only when that came back empty. For a DELETE_FAILED
    # stack StackStatusReason is NEVER empty — it is literally "The following
    # resource(s) failed to delete: [...]" — so the branch reading the actual
    # per-resource ResourceStatusReason could not execute, and an operator whose
    # teardown left eleven stuck resources was shown the list of resources and
    # no cause at all. A reachability inversion, not a missing feature: the
    # fallback was correct and unreachable. There is no fallback now, so there
    # is no branch left to invert.
    #
    # Both calls are best-effort (`2>/dev/null || true`). A reason lookup that
    # itself fails must not mask the delete failure it is trying to explain, and
    # the library's composition states an absence rather than printing a blank.
    stack_reason=""
    stack_reason="$(aws cloudformation describe-stacks \
      --stack-name "$physical" \
      --region "$region" \
      --profile "$PROFILE" \
      --query 'Stacks[0].StackStatusReason' \
      --output text 2>/dev/null || true)"

    # ALL DELETE_FAILED events, as `LogicalResourceId<TAB>ResourceStatusReason`
    # rows — not `|[0]`, which was a truncation by another name: eleven failed
    # resources reported as one (p64 R2.4). The two-field text projection is the
    # same idiom the policy enumeration above uses for
    # `Policies[].[PolicyName,Arn]`, cited by its query rather than by a line
    # number so this comment cannot drift the way LGR-4ea43f3-5 records.
    failed_events=""
    failed_events="$(aws cloudformation describe-stack-events \
      --stack-name "$physical" \
      --region "$region" \
      --profile "$PROFILE" \
      --query "StackEvents[?ResourceStatus=='DELETE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
      --output text 2>/dev/null || true)"

    # Parsing these rows is pure, so it lives in the library where it can be
    # exercised directly instead of only through a run that deletes real
    # resources (LGR-b5b76ed-1). This driver does the two API calls and the
    # reporting; destroy_lib.sh does the extraction and the composition.
    reason_report=""
    reason_report="$(printf '%s\n' "$failed_events" | compose_failure_reason "$stack_reason")"

    # One id per line, read into an array rather than word-split from a string
    # — the shape failed_resource_ids documents as its interface. Expanding a
    # space-separated string unquoted would make correctness depend on IFS.
    RETAIN_IDS=()
    while IFS= read -r retain_id; do
      [ -n "$retain_id" ] || continue
      RETAIN_IDS+=("$retain_id")
    done < <(printf '%s\n' "$failed_events" | failed_resource_ids)

    say ""
    if [ "${#RETAIN_IDS[@]}" -gt 0 ]; then
      # The recovery the reporter of this defect performed by hand, printed so
      # it can be performed by hand again — and so that an operator reading the
      # transcript can see exactly what the automatic attempt below issued.
      say "    ${#RETAIN_IDS[@]} resource(s) failed to delete. Retry retaining them with:"
      say "      aws cloudformation delete-stack --stack-name $physical --region $region --profile $PROFILE --retain-resources ${RETAIN_IDS[*]}"
      say ""

      # ── p64 R2.1: issue that retry, automatically, ONCE ───────────────
      #
      # RETAIN_IDS is expanded "${RETAIN_IDS[@]}", never unquoted and never as
      # a space-joined string. The one-id-per-line shape failed_resource_ids
      # documents exists precisely so this call does not depend on IFS; the
      # ${RETAIN_IDS[*]} above is a human-readable hint and this is the API
      # call, and they must not be confused for each other. The array is
      # non-empty inside this guard, which is what makes "${RETAIN_IDS[@]}"
      # safe under `set -u`.
      #
      # ONCE, and not a loop. Three reasons, in increasing order of weight:
      #
      #   * There is nothing left to escalate to. The retain list is complete
      #     by construction — it is every DELETE_FAILED resource the events
      #     query reported — so a second identical request asks the same
      #     question and gets the same answer.
      #   * The failures --retain-resources addresses are not transient. A
      #     non-empty bucket, a role still in use, a live dependency: none
      #     resolves by waiting, so backoff buys nothing. A genuinely
      #     transient failure would have shown up on the plain delete above.
      #   * Each attempt ABANDONS resources. A loop would abandon a growing
      #     set of live infrastructure with no operator in the loop, which is
      #     the opposite of what a retry is for.
      #
      # Deliberately NOT --deletion-mode FORCE_DELETE_STACK, which is the other
      # documented way out of DELETE_FAILED. It removes the stack regardless of
      # which resources failed, without anything enumerating what it abandoned —
      # so the abandonment report below could only be a guess. --retain-resources
      # requires naming each id, and the id grammar in destroy_lib.sh is what
      # keeps a non-id out of that list, so what is abandoned is exactly what is
      # reported.
      item "retrying $physical, retaining ${#RETAIN_IDS[@]} resource(s)"
      retry_err=""
      if ! retry_err="$(aws cloudformation delete-stack \
            --stack-name "$physical" \
            --region "$region" \
            --profile "$PROFILE" \
            --retain-resources "${RETAIN_IDS[@]}" 2>&1 >/dev/null)"; then
        # CloudFormation refused the request itself. Nothing was abandoned.
        printf '%s\n' "${RETAIN_IDS[@]}" \
          | compose_retain_retry_outcome rejected "$physical" "$retry_err" \
          || die "internal: could not compose the retry report for $physical"
        say ""
      # ── R2.1's re-wait ────────────────────────────────────────────────
      #
      # delete-stack returns as soon as the request is accepted, so without this
      # the retry would report nothing but "we asked again". Worse than nothing:
      # a retain-resources delete that is accepted and then fails AGAIN is the
      # common case when the retain list is not sufficient to free the stack, so
      # "accepted" and "worked" are genuinely different outcomes here and only
      # the waiter can tell them apart.
      elif aws cloudformation wait stack-delete-complete \
            --stack-name "$physical" \
            --region "$region" \
            --profile "$PROFILE" >/dev/null 2>&1; then
        printf '%s\n' "${RETAIN_IDS[@]}" \
          | compose_retain_retry_outcome retained "$physical" \
          || die "internal: could not compose the retry report for $physical"
        # Recorded, not just printed. An abandoned resource is the one thing a
        # closing "Teardown complete" must not silently cover, so the summary
        # names it — the same reason record_absence exists, for the opposite
        # fact. Absent means gone; retained means still there and unmanaged.
        RETAINED+=("$physical ($region): ${RETAIN_IDS[*]}")
        say ""
        # The stack is gone, so the next stack's delete is safe to request:
        # this is the one path past a DELETE_FAILED that does not violate the
        # dependency ordering. `i` was already incremented at the top of the
        # loop body, so `continue` advances rather than repeating.
        #
        # This is NOT the continue-past-failure mode R2.2 asks for, which now
        # exists as --continue-past-failed-stacks. That mode continues past a
        # stack that is STILL STANDING; this continues past one that is deleted,
        # which is why it needs no flag and records into RETAINED rather than
        # SKIPPED_STACKS. Both distinctions are load-bearing: this arm keeps the
        # dependency ordering intact and exits 0, that one breaks the ordering
        # and exits non-zero. The default posture is unchanged either way —
        # without the flag, every arm below this one still stops.
        continue
      else
        printf '%s\n' "${RETAIN_IDS[@]}" \
          | compose_retain_retry_outcome failed "$physical" \
          || die "internal: could not compose the retry report for $physical"
        say ""
      fi
    fi
    # R2.2's opt-in arm, and the one it was written for: the plain delete was
    # accepted, the waiter says the stack did not go, and the retain-resources
    # retry above either was not applicable or did not free it. So the stack is
    # standing WITH some subset of its resources still in it — the case where the
    # cascade the notice warns about is most likely, because whatever the next
    # stack needs gone may be exactly what is stuck here.
    #
    # `reason_report` is passed whole. It is the composed per-resource report
    # (R2.3, R2.4) and the single most useful thing an operator gets out of this
    # run; the notice prints it line by line and truncates nothing.
    if [ "$CONTINUE_PAST_FAILED_STACKS" -eq 1 ]; then
      skip_failed_stack "$physical" "$region" \
        "CloudFormation reports:
$reason_report"
      continue
    fi
    # The reason block is the LAST thing in the message, not spliced mid
    # sentence, because it is multi-line by design and a trailing clause after
    # it would read as part of the last resource's reason.
    die "deletion of stack $physical ($region) did not complete. Stopped — no subsequent stack deletion was requested. CloudFormation reports:
$reason_report"
  fi
  item "deleted  $physical"
done

# ── Delete the prefixed tables (R2.2, R2.3) ───────────────────────────
#
# R2.2: AFTER the stacks. A table the Data stack still references would be
# recreated by a stack whose deletion is in flight, and deleting it first turns
# a clean teardown into a race. The RETAIN tables outlive the stack by design,
# so there is nothing to gain from going first.
if [ "${#TABLES[@]}" -gt 0 ]; then
  say ""
  say "==> Deleting DynamoDB tables (${#TABLES[@]}) in $REGION"
  for name in "${TABLES[@]}"; do
    # `2>&1 >/dev/null` and not a temp file: the redirections apply left to
    # right, so stderr goes to the command substitution and stdout is
    # discarded. A temp file here would put a side effect in whatever directory
    # the operator happened to run this from.
    table_err=""
    if ! table_err="$(aws dynamodb delete-table \
          --table-name "$name" \
          --region "$REGION" \
          --profile "$PROFILE" 2>&1 >/dev/null)"; then
      if is_absence_error "$table_err"; then
        record_absence "table $name"
        continue
      fi
      say ""
      die "deletion of table $name failed: ${table_err%%$'\n'*}. This is not an absence, so it is not continued past."
    fi
    item "deleted  table $name"
  done
fi

# ── Delete the prefixed IAM roles and policies (R3.2, R3.3, R3.4) ─────
#
# Roles before policies: deleting a role removes its attachments, so doing
# roles first is what leaves the customer-managed policies detached enough to
# be deletable.
if [ "${#ROLES[@]}" -gt 0 ]; then
  say ""
  say "==> Deleting IAM roles (${#ROLES[@]}) — account-global, not scoped by $REGION"
  for name in "${ROLES[@]}"; do
    # R3.2 then R3.3, both BEFORE the delete: IAM refuses to delete a role that
    # still carries either an attached managed policy or an inline one, and it
    # refuses with an error that names neither. Doing both first turns what
    # would be an opaque DeleteConflict into an ordinary success.
    # `--output text` returns a flat list tab-separated on one line, and the
    # unquoted expansion below word-splits it. That is safe for these three
    # lists specifically — a policy ARN, an inline policy name and a policy
    # version id cannot contain whitespace — and is why the table and role name
    # lists above go through select_prefixed line-wise instead.
    attached=""
    attached="$(aws iam list-attached-role-policies \
      --role-name "$name" \
      --region "$REGION" \
      --profile "$PROFILE" \
      --query 'AttachedPolicies[].PolicyArn' \
      --output text 2>/dev/null || true)"
    for arn in $attached; do
      [ -n "$arn" ] || continue
      [ "$arn" != "None" ] || continue
      detach_err=""
      if ! detach_err="$(aws iam detach-role-policy \
            --role-name "$name" \
            --policy-arn "$arn" \
            --region "$REGION" \
            --profile "$PROFILE" 2>&1 >/dev/null)"; then
        if is_absence_error "$detach_err"; then
          record_absence "attachment $name -> $arn"
          continue
        fi
        say ""
        die "detaching $arn from role $name failed: ${detach_err%%$'\n'*}. This is not an absence, so it is not continued past."
      fi
      item "detached $arn from $name"
    done

    inline=""
    inline="$(aws iam list-role-policies \
      --role-name "$name" \
      --region "$REGION" \
      --profile "$PROFILE" \
      --query 'PolicyNames[]' \
      --output text 2>/dev/null || true)"
    for policy_name in $inline; do
      [ -n "$policy_name" ] || continue
      [ "$policy_name" != "None" ] || continue
      inline_err=""
      if ! inline_err="$(aws iam delete-role-policy \
            --role-name "$name" \
            --policy-name "$policy_name" \
            --region "$REGION" \
            --profile "$PROFILE" 2>&1 >/dev/null)"; then
        if is_absence_error "$inline_err"; then
          record_absence "inline policy $name/$policy_name"
          continue
        fi
        say ""
        die "deleting inline policy $policy_name on role $name failed: ${inline_err%%$'\n'*}. This is not an absence, so it is not continued past."
      fi
      item "deleted  inline policy $name/$policy_name"
    done

    role_err=""
    if ! role_err="$(aws iam delete-role \
          --role-name "$name" \
          --region "$REGION" \
          --profile "$PROFILE" 2>&1 >/dev/null)"; then
      if is_absence_error "$role_err"; then
        record_absence "role $name"
        continue
      fi
      say ""
      die "deletion of role $name failed: ${role_err%%$'\n'*}. This is not an absence, so it is not continued past."
    fi
    item "deleted  role $name"
  done
fi

if [ "${#POLICY_ARNS[@]}" -gt 0 ]; then
  say ""
  say "==> Deleting customer-managed IAM policies (${#POLICY_ARNS[@]}) — account-global"
  for arn in "${POLICY_ARNS[@]}"; do
    # A managed policy with more than one version cannot be deleted while the
    # non-default versions exist, and IAM says so with a DeleteConflict that
    # does not mention versions. Every redeploy that changed a policy document
    # left another version behind, so on a real account this is the common case
    # rather than the edge one.
    versions=""
    # The backticks are JMESPath's literal syntax, not command substitution, so
    # the single quotes are correct and SC2016 is a false positive here —
    # switching to double quotes is what would break this query.
    # shellcheck disable=SC2016
    versions="$(aws iam list-policy-versions \
      --policy-arn "$arn" \
      --region "$REGION" \
      --profile "$PROFILE" \
      --query 'Versions[?IsDefaultVersion==`false`].VersionId' \
      --output text 2>/dev/null || true)"
    for version in $versions; do
      [ -n "$version" ] || continue
      [ "$version" != "None" ] || continue
      version_err=""
      if ! version_err="$(aws iam delete-policy-version \
            --policy-arn "$arn" \
            --version-id "$version" \
            --region "$REGION" \
            --profile "$PROFILE" 2>&1 >/dev/null)"; then
        if is_absence_error "$version_err"; then
          record_absence "policy version $arn/$version"
          continue
        fi
        say ""
        die "deleting version $version of policy $arn failed: ${version_err%%$'\n'*}. This is not an absence, so it is not continued past."
      fi
    done

    policy_err=""
    if ! policy_err="$(aws iam delete-policy \
          --policy-arn "$arn" \
          --region "$REGION" \
          --profile "$PROFILE" 2>&1 >/dev/null)"; then
      if is_absence_error "$policy_err"; then
        record_absence "policy $arn"
        continue
      fi
      say ""
      die "deletion of policy $arn failed: ${policy_err%%$'\n'*}. This is not an absence, so it is not continued past."
    fi
    item "deleted  policy $arn"
  done
fi

# ── Summary ───────────────────────────────────────────────────────────
#
# Recorded absences are reprinted rather than folded into the success line: a
# run that found nine of seventeen tables already gone is a successful run, but
# it is not the same run as one that deleted all seventeen, and an operator
# reading only the last line should not have to guess which they got.
say ""
if [ "${#ABSENT[@]}" -gt 0 ]; then
  say "Recorded ${#ABSENT[@]} absent resource(s) — expected, and continued past:"
  for a in ${ABSENT[@]+"${ABSENT[@]}"}; do item "$a"; done
fi
# Printed AFTER the absences and BEFORE the completion line, because it is the
# one part of this summary that names resources still standing in the account.
if [ "${#RETAINED[@]}" -gt 0 ]; then
  say "RETAINED by a retain-resources retry on ${#RETAINED[@]} stack(s) — STILL LIVE in account"
  say "$ACCOUNT and no longer managed by CloudFormation. Delete by hand, or the next deploy"
  say "collides with these fixed physical names:"
  for r in ${RETAINED[@]+"${RETAINED[@]}"}; do item "$r"; done
fi

# ── R2.2: the skipped stacks, named, and a non-zero exit ──────────────
#
# LAST in the summary and it takes the exit code with it, because this is the one
# outcome under which "Teardown complete" would be false. A run that walked past
# a standing stack did not tear the system down: those stacks are still there,
# everything in them is still there and still billed, and a redeploy will collide
# with their fixed physical names. So the completion line is not printed at all on
# this path — replaced, not decorated — and the run exits non-zero.
#
# Exit 1, the same code `die` uses, and deliberately not a third code. R2.2's
# requirement is that the exit be non-zero, so that CI cannot read a teardown
# that skipped stacks as a clean one; 1 is already this script's spelling of "the
# teardown did not do what it set out to do". A distinct code — 3, say, since 2 is
# taken by the usage errors — was considered and rejected: it would be a new
# contract for every caller to learn in exchange for a distinction no caller in
# this repo makes, and the transcript already separates the two cases far more
# legibly than an integer can. A caller that genuinely needs to tell "stopped at
# the first failure" from "continued and skipped" has a simpler signal available:
# it passed the flag or it did not.
#
# Residual, recorded rather than fixed: `die` exits immediately, so a table or
# IAM deletion that hard-fails AFTER a skipped stack terminates the run before
# this summary prints, and the skip goes unnamed. RETAINED has carried exactly
# that property since it was added, and giving one of the three arrays a
# flush-on-die that the others do not have would be the worse outcome — the fix
# is a summary-on-exit trap covering all three, which is a change to every
# failure arm in this script and belongs to whoever wants it, not to this flag.
# The at-the-moment notice is printed the instant the skip happens, so the
# information is never lost from the transcript, only from the summary.
if [ "${#SKIPPED_STACKS[@]}" -gt 0 ]; then
  say "SKIPPED ${#SKIPPED_STACKS[@]} stack(s) that did NOT delete, because"
  say "--continue-past-failed-stacks was passed. These are STILL STANDING in account"
  say "$ACCOUNT, with every resource still in them:"
  for s in ${SKIPPED_STACKS[@]+"${SKIPPED_STACKS[@]}"}; do item "$s"; done
  say ""
  say "Any stack deletion reported as failed AFTER one of these may have failed BECAUSE"
  say "of it: stacks are deleted in dependency order, so a skipped stack still holds"
  say "whatever the ones after it were waiting on. Treat the run above as one cause with"
  say "several symptoms and re-run the teardown once these are resolved."
  say ""
  say "Teardown INCOMPLETE for account $ACCOUNT, region $REGION, environment $ENV_NAME —"
  say "${#SKIPPED_STACKS[@]} stack(s) skipped."
  exit 1
fi
say "Teardown complete for account $ACCOUNT, region $REGION, environment $ENV_NAME."
exit 0
