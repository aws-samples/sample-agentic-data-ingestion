#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# destroy_lib.sh — pure helpers for this system's teardown computations.
#
# Sourceable and side-effect-free: this file only defines things. Nothing runs
# at source time, nothing reads or writes the caller's state, nothing deletes
# anything, and nothing writes to stdout except a function's own resolved
# value. That is what makes the logic exercisable directly:
#
#   bash -c '. scripts/destroy_lib.sh; stack_deletion_order'
#
# ...instead of only through a teardown that deletes real resources. Turning a
# resolved value into an API call, and turning a failure into a user-facing
# message, stays in destroy.sh. This mirrors scripts/deploy_prereq_lib.sh
# exactly, per LGR-b5b76ed-1: the pure computations are the tested surface and
# the entrypoint is a thin driver over them.
#
# Usage (from destroy.sh, after SCRIPT_DIR is set):
#   # shellcheck source=scripts/destroy_lib.sh
#   . "$SCRIPT_DIR/scripts/destroy_lib.sh"
#
# ── The interface, stated once so callers need not infer it ───────────
#
#   stack_deletion_order                     -> 9 logical names, one per line
#   stack_region <logical> <operator_region> -> one region name
#   select_prefixed <prefix>   (names on stdin) -> the selected subset
#   failed_resource_ids        (event rows on stdin) -> logical ids, one per line
#   compose_failure_reason <stack_reason>  (event rows on stdin) -> a report block
#   compose_retain_retry_outcome <outcome> <stack> [detail]
#                              (retained ids on stdin) -> a report block
#   compose_skipped_stack_notice <stack> <region> [detail] -> a report block
#   select_agentcore_runtimes <prefix>  (name<TAB>id rows on stdin)
#                                            -> the selected rows
#   compose_agentcore_sweep_refusal <stack> <region> [status] -> a report block
#   resolve_account <profile>                -> one 12-digit account id
#
# Every function writes its value to stdout and nothing else; every failure is
# a non-zero return with empty stdout. Callers test the return code, never the
# emptiness of the output.
#
# ── TWO drivers source this, not one ──────────────────────────────────
#
# `destroy.sh` is the main one. `scripts/cleanup_agentcore_runtimes.sh` (p64
# 1b.4) is the second: a standalone helper that clears AgentCore RUNTIMES a
# failed teardown abandoned. Its computations live here for the same reason
# destroy.sh's do (`LGR-b5b76ed-1`) — they are pure, and a sweep that deletes
# AI agent runtimes should be exercisable without deleting one.
#
# Sharing this library is NOT the same as wiring that helper into destroy.sh,
# and the distinction matters because the helper deliberately is not wired in
# (see `select_agentcore_runtimes` for the argument). destroy.sh gains two
# function definitions in scope and calls neither; nothing in destroy.sh's
# control flow reaches an AgentCore runtime delete.
#
# ── Logical names, not physical ones ──────────────────────────────────
#
# `stack_deletion_order` and `stack_region` both speak in *logical* names —
# `Hosting`, `EdgeWaf`, `Data` — not in deployed stack names. The deployed name
# is `AgenticDataIngestion-<Logical>-<env>` (e.g. `AgenticDataIngestion-Hosting-dev`),
# where `<env>` is the `environment` CDK context key and defaults to `dev`
# (`deploy-cdk/bin/agentic_data_ingestion.ts:119`). Composing that string is
# destroy.sh's job, because `<env>` is an operator input this library has no
# business defaulting.
#
# Logical names are the right unit for three reasons. They are what
# `stack_region` can match *exactly* — `[ "$1" = EdgeWaf ]` cannot misfire the
# way a substring test against a deployed name could. They are stable across
# environments, so the order is not re-derived per `--environment`. And they
# make the stack-set comparison test a name-set comparison against the
# `new <Logical>Stack(` constructions in `bin/agentic_data_ingestion.ts`,
# rather than a comparison that has to reconstruct an env suffix first.
#
# ── No account identifier appears anywhere in this file ───────────────
#
# Deliberate, and load-bearing. `LGR-1166391-2` records that a hardcoded
# account was copied verbatim into a deploy aimed at the *other* of this
# system's two accounts and updated a Data stack in the wrong one. The account
# is always derived from the operator's profile via `resolve_account` and
# echoed before anything is touched (R1.3). A teardown tool carrying the same
# trap would **destroy** in the wrong account rather than update, so there is
# no literal here to copy and no default to fall back to.

# ── Stack deletion order (R1.6) ───────────────────────────────────────
#
# Derived from the dependency graph in `deploy-cdk/bin/agentic_data_ingestion.ts`
# (constructions verified at :128, :135, :141, :147, :159, :188, :203, :210,
# :235), NOT from the four-stack deploy order documented in
# `.kiro/steering/project-overview.md` — that order (Auth → Compute → EdgeWaf →
# Hosting) is a deploy convenience covering four of the nine and is not the
# graph.
#
# Declared stacks and their explicit `addDependency` edges:
#
#   foundation    — no deps
#   data          — no deps
#   authStack     — no deps
#   agentcore     — foundation
#   compute       — foundation, data, agentcore, authStack
#   orchestration — compute, data, foundation
#   edgeWaf       — no deps; env.region pinned 'us-east-1'
#   hosting       — compute, authStack, edgeWaf
#   testFixtures  — context-gated on `adi:deployTestFixtures`
#
# Deletion is the reverse of a topological creation order, so a stack is only
# deleted once everything depending on it is gone. Per-step reasons:
#
#   TestFixtures  first, because it is context-gated and may hold VPC
#                 resources that other stacks' deletions would wait on.
#   Hosting       before EdgeWaf, because Hosting consumes EdgeWaf's WAF ARN
#   EdgeWaf       through `crossRegionReferences: true`; deleting the producer
#                 of a live export fails on the export, so the consumer goes
#                 first. This is also the only entry `stack_region` moves.
#   Compute       after Orchestration, which depends on it.
#   AgentCore     after Compute.
#   Auth          after Compute and Hosting.
#   Data          after Compute and Orchestration. This is the stack carrying
#                 `terminationProtection: true` (`data-stack.ts:33`, verified by
#                 grep at commit e5e9edd on a clean tree — which is what both
#                 spec documents say; an earlier revision of this comment
#                 claimed :32, and :32 is the `constructor(` line above it),
#                 so R1.8's protection-disable fires on this entry.
#   Foundation    last: AgentCore, Compute and Orchestration all depend on it.
#
# NINE entries, not eight. design's `## Components and Interfaces` table says
# "the eight stack logical names" while design §4.1's own enumeration lists
# nine — the eight always-deployed stacks plus the context-gated TestFixtures.
# The nine-entry reading is the one taken here, for two reasons. It is the
# reading §4.1 actually enumerates, and it is the only reading under which the
# stack-set comparison test is satisfiable: that test compares this set against
# the `new *Stack(` constructions in `bin/agentic_data_ingestion.ts`, of which
# there are nine (TestFixtures constructed at :235 inside the context gate).
#
# TestFixtures being context-gated is a fact about *deployment*, not about this
# list. A gated stack is either deployed or it is not, and this function cannot
# know which — the context flag was supplied to a past `cdk deploy`, not to
# this teardown. So it is always emitted, and destroy.sh discovers whether the
# stack actually exists when it enumerates. That keeps absence a recorded
# non-event rather than a name this tool has no vocabulary for: omitting it
# would make an opt-in stack undeletable by the only tool built to delete it.
#
# A function rather than a literal list in destroy.sh, per design decision
# W1-D2: the order is derivable from the CDK entrypoint, and a transcribed list
# goes stale silently the first time a stack is added — the drift
# `LGR-28b4d75-5` records ("a test written from the producer's output cannot
# detect the producer drifting"). Being a function is what lets a test compare
# the set against the producer.
ADI_STACK_DELETION_ORDER=(
  TestFixtures
  Hosting
  Orchestration
  EdgeWaf
  Compute
  AgentCore
  Auth
  Data
  Foundation
)

# stack_deletion_order — echo the stack logical names, newest-dependent first.
#
# stdout: one logical stack name per line, in deletion order. Always 9 lines.
# return: 0.
#
#   $ stack_deletion_order
#   TestFixtures
#   Hosting
#   ...
#   Foundation
#
# Takes no arguments: the order is a property of the CDK app's dependency
# graph, not of the operator's invocation. Region and environment enter through
# `stack_region` and through destroy.sh's name composition respectively.
stack_deletion_order() {
  local name
  for name in "${ADI_STACK_DELETION_ORDER[@]}"; do
    printf '%s\n' "$name"
  done
}

# stack_region — echo the region a given stack must be addressed in (R1.7).
#
# $1: a logical stack name, as emitted by `stack_deletion_order`.
# $2: the operator-supplied region.
# stdout: `us-east-1` for EdgeWaf, `$2` for every other stack.
# return: 0 on success; 2 when either argument is empty (no stdout).
#
#   $ stack_region Hosting us-west-2
#   us-west-2
#   $ stack_region EdgeWaf us-west-2
#   us-east-1
#
# EdgeWaf is pinned because a CLOUDFRONT-scope WebACL can only exist in
# us-east-1, so that is where the stack was created
# (`bin/agentic_data_ingestion.ts:204` passes `region: 'us-east-1'` explicitly)
# and the only region a delete request for it can be addressed to. Addressing
# it in the operator's region gets a "stack does not exist" that reads as
# "already gone" — the failure mode this special case exists to prevent.
#
# An empty region is a hard failure rather than a fallback. There is no sane
# default: falling back to the caller's ambient AWS_REGION would send delete
# requests at whatever region the shell happens to be configured for, which is
# the same class of mistake as a hardcoded account. Two required arguments and
# a non-zero return keep the region an explicit operator input.
#
# `${1:-}` rather than `$1` so the function returns 2 under `set -u` instead of
# aborting the caller's shell — the test harness sources this library with
# `set -uo pipefail`, and a testable failure arm has to be a return code.
stack_region() {
  local name="${1:-}" operator_region="${2:-}"
  [ -n "$name" ] || return 2
  [ -n "$operator_region" ] || return 2
  if [ "$name" = "EdgeWaf" ]; then
    printf '%s\n' "us-east-1"
  else
    printf '%s\n' "$operator_region"
  fi
}

# select_prefixed — echo the stdin names that begin with $1 (R2.1, R2.4, R3.1).
#
# $1: the required prefix.
# stdin: a newline-separated list of resource names.
# stdout: the input lines beginning with $1, in input order, one per line.
# return: 0 on success (including when nothing is selected); 2 when $1 is
#         empty (no stdout).
#
#   $ printf 'agentic_data_ingestion_state\nsomeone_elses_table\n' \
#       | select_prefixed agentic_data_ingestion
#   agentic_data_ingestion_state
#
# This is the one function whose *exclusions* are the safety property. R2.4 and
# R3.1 run against a possibly-shared account, so a name this tool does not
# recognise must not be selected: everything selected here is a deletion
# candidate, and there is no undo. An empty result is therefore a normal,
# successful outcome (R1.10's empty account), never an error.
#
# Three deliberate choices, each closing a way to over-select:
#
#   * An empty prefix returns 2 instead of selecting nothing. Both readings
#     are defensible in isolation and only one is safe to get wrong: a caller
#     whose prefix variable was accidentally empty gets a loud failure rather
#     than a silently empty enumeration that reads as "nothing to delete".
#     The complementary hazard — an empty prefix matching *everything* — is
#     what the failure also forecloses.
#
#   * Literal prefix comparison via `${line#"$prefix"}`, not `grep "^$prefix"`
#     and not `case "$line" in "$prefix"*)`. `grep` would read the prefix as a
#     regex and `case` glob-expands its pattern, so in both a prefix
#     containing `.`, `*`, `?` or `[` matches names it should not. The quoted
#     expansion inside `${…#…}` suppresses pattern interpretation, making this
#     an exact literal-prefix test for any prefix whatsoever.
#
#   * Blank lines are skipped. `aws --output text` emits an empty line for an
#     empty result set, and an empty name selected as a deletion candidate is
#     a malformed API call at best.
#
# The prefix is an argument rather than a baked-in constant because there are
# two variants of it — `agentic_data_ingestion` and `agentic-data-ingestion` —
# and destroy.sh applies both. Keeping the variants in the caller leaves this
# function a single-purpose filter with nothing to keep in sync.
#
# `|| [ -n "$line" ]` catches a final line with no trailing newline, which
# `read` reports as failure after having populated `line`.
select_prefixed() {
  local prefix="${1:-}" line
  [ -n "$prefix" ] || return 2
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    [ "${line#"$prefix"}" != "$line" ] || continue
    printf '%s\n' "$line"
  done
}

# ── Reporting a stack that reached DELETE_FAILED (p64 R2.1, R2.3, R2.4) ───
#
# Both functions below read the SAME input: the `--output text` rendering of
#
#   describe-stack-events --query \
#     "StackEvents[?ResourceStatus=='DELETE_FAILED'].[LogicalResourceId,ResourceStatusReason]"
#
# which is one row per failed-delete event, `LogicalResourceId<TAB>Reason`. The
# two-field text projection is the idiom `destroy.sh` already relies on for its
# policy enumeration (`Policies[].[PolicyName,Arn]`, split on `$'\t'`), so this
# adds no new assumption about the CLI's output format.
#
# ALL matching events, deliberately, not `|[0]`. Asking for the first event was
# a truncation by another name: the reported teardown had ELEVEN resources fail
# in one stack and the operator was shown one reason. p64 R2.4 is the general
# form of that — nothing on this path may drop text, because the informative
# part of a CloudFormation reason is regularly past the first line.
#
# Why two functions over one input rather than one function returning both: the
# ids feed an API argument (`delete-stack --retain-resources`) and the reasons
# feed a human. Their failure modes are opposite. An id that should not have
# been selected becomes a malformed API call, so id extraction must UNDER-select
# when in doubt; a reason line that is not recognised must still be shown, so
# composition must NEVER drop input. One function cannot hold both postures.

# _looks_like_logical_id — is $1 shaped like a CloudFormation logical id?
#
# Internal to the two functions below. CloudFormation restricts a logical
# resource id to alphanumerics, so the character-class test is exact rather
# than heuristic, and it is the safety property of `failed_resource_ids`:
# anything that is not an id cannot reach `--retain-resources`.
#
# `None` is excluded explicitly even though it is alphanumeric. `aws --output
# text` prints `None` for a null field, and `destroy.sh` already guards every
# other text-mode list against that spelling ([ "$arn" != "None" ]); matching
# that convention here keeps the defence in one recognisable shape.
#
# A consequence worth naming: the stack's own DELETE_FAILED event carries the
# DEPLOYED stack name as its LogicalResourceId, and that name is always
# `AgenticDataIngestion-<Logical>-<env>` — hyphens, so it fails this test and is
# excluded from the retain set. That is correct (a stack cannot retain itself)
# but it is a consequence of the id grammar, not a check for it. Residual: a
# hypothetical single-token stack name would pass, and CloudFormation would
# reject `--retain-resources <stackname>` loudly. Loud and harmless is the right
# side of that trade; a silent inclusion would not be.
_looks_like_logical_id() {
  local candidate="${1:-}"
  [ -n "$candidate" ] || return 1
  [ "$candidate" != "None" ] || return 1
  case "$candidate" in
    *[!A-Za-z0-9]*) return 1 ;;
  esac
  return 0
}

# failed_resource_ids — echo the logical ids of the resources that failed to
# delete (R2.1).
#
# stdin: the event rows described in the section header above.
# stdout: one logical resource id per line, in first-seen order, de-duplicated.
# return: 0, including when nothing is selected — an empty selection is the
#         normal outcome for a stack that failed for a non-resource reason.
#
#   $ printf 'RoleA\tCannot delete\nRoleA\tCannot delete\n' | failed_resource_ids
#   RoleA
#
# ── The output shape is an interface, and it is one-id-per-line ────────
#
# `delete-stack --retain-resources` takes a space-separated list on the command
# line, so a caller is tempted to emit one space-separated string and expand it
# unquoted. Do not: that makes the caller's correctness depend on word
# splitting, which is the class of bug `select_prefixed` documents at length.
# One value per line is also what `stack_deletion_order` emits, so callers have
# one convention rather than two. The caller reads it into an array and expands
# with `"${arr[@]}"`:
#
#   RETAIN_IDS=()
#   while IFS= read -r id; do RETAIN_IDS+=("$id"); done < <(… | failed_resource_ids)
#   aws cloudformation delete-stack … --retain-resources "${RETAIN_IDS[@]}"
#
# ── De-duplication is required, not tidiness ──────────────────────────
#
# `describe-stack-events` returns the stack's whole event history, newest
# first, so a stack whose deletion was attempted more than once carries a
# DELETE_FAILED event per attempt per resource. `--retain-resources` rejects a
# repeated id, so passing the raw list would turn a recoverable failure into an
# invalid request. First-seen order is kept so the ids read in the order
# CloudFormation last reported them.
#
# ── Two independent filters, because over-selection here is an API call ─
#
# A row must contain a tab AND its first field must be a well-formed logical
# id. The tab test is what excludes a continuation line of a multi-line reason:
# a real row always has two fields, a wrapped reason line has none. The id test
# then excludes anything that reached the first field but is not an id. Either
# alone would be weaker than the pair — a reason containing a tab would have to
# also begin with a bare alphanumeric word to slip through both, and that
# residual is stated rather than papered over.
failed_resource_ids() {
  local line id seen=""
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    id="${line%%$'\t'*}"
    # Equal means no tab was found, so this is not an event row.
    [ "$id" != "$line" ] || continue
    _looks_like_logical_id "$id" || continue
    case " $seen " in *" $id "*) continue ;; esac
    seen="$seen $id"
    printf '%s\n' "$id"
  done
}

# compose_failure_reason — echo the operator-facing reason report (R2.3, R2.4).
#
# $1: the stack-level `StackStatusReason`, as `--output text` rendered it
#     (`None` and empty are both accepted and reported as an absence).
# stdin: the event rows described in the section header above.
# stdout: a multi-line report — the stack-level reason on the first line, then
#         one indented `<id>: <reason>` line per failed resource.
# return: 0.
#
#   $ printf 'RoleA\tCannot delete entity\n' \
#       | compose_failure_reason 'The following resource(s) failed to delete: [RoleA].'
#   The following resource(s) failed to delete: [RoleA].
#     RoleA: Cannot delete entity
#
# ── Both, not one falling back to the other ───────────────────────────
#
# The two inputs answer different questions and the report needs both. The
# stack-level reason names WHICH resources failed; the per-resource reasons say
# WHY each one did. `destroy.sh` previously asked for the stack reason and
# consulted the events only when it came back empty — and for a DELETE_FAILED
# stack it is never empty, so the branch reading the real cause was
# unreachable. The operator got the list of stuck resources and no cause. This
# function takes both unconditionally so that inversion cannot recur: there is
# no branch here to invert.
#
# ── Nothing is dropped, including what this function cannot parse ─────
#
# A line with no tab is a continuation of a multi-line ResourceStatusReason, and
# it is emitted at a deeper indent rather than skipped. That is the whole of
# R2.4: an unparsed line is a formatting problem, a dropped line is a lost
# cause. The complementary posture to `failed_resource_ids`, which under-selects
# for the opposite reason.
#
# ── This function does NOT apply the logical-id grammar ───────────────
#
# Deliberate, and it took a rendering bug to see why. Reusing
# `_looks_like_logical_id` here sent the stack's OWN DELETE_FAILED event — whose
# LogicalResourceId is the hyphenated deployed stack name, so not an id by that
# grammar — down the continuation arm, where it rendered as a wrapped fragment
# of the preceding resource's reason. A row this function cannot RETAIN is still
# a row it can READ. The grammar exists to keep a bad argument out of
# `--retain-resources`, which is a concern this function does not have, so the
# only test here is "does the line have two fields".
#
# Absences are stated rather than left blank, in both positions. A blank line
# where a reason should be reads as a tool that did not look.
compose_failure_reason() {
  local stack_reason="${1:-}" line label detail rows=0
  case "$stack_reason" in
    ""|None) printf '%s\n' "no stack-level reason reported by CloudFormation" ;;
    *) printf '%s\n' "$stack_reason" ;;
  esac
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    label="${line%%$'\t'*}"
    # Equal means no tab was found: a continuation line, indented and kept.
    if [ "$label" = "$line" ]; then
      printf '      %s\n' "$line"
      continue
    fi
    detail="${line#*$'\t'}"
    case "$detail" in ""|None) detail="no per-resource reason reported by CloudFormation" ;; esac
    printf '  %s: %s\n' "$label" "$detail"
    rows=$(( rows + 1 ))
  done
  [ "$rows" -gt 0 ] || printf '%s\n' "  no DELETE_FAILED resource event reported by CloudFormation"
}

# compose_retain_retry_outcome — echo the report for the one retain-resources
# retry destroy.sh issues after a DELETE_FAILED stack (p64 R2.1).
#
# $1: the outcome — exactly one of `retained`, `rejected`, `failed`.
# $2: the deployed stack name the retry was addressed to.
# $3: optional CLI detail (stderr, or any explanatory text). Printed whole.
# stdin: the retained logical ids, one per line, as `failed_resource_ids` emits
#        them. Read for the list AND for the count, so the two cannot disagree.
# stdout: an indented report block, 4-space body and 6-space detail, matching
#         the `item`-level stream destroy.sh prints it into.
# return: 0 on success; 2 when $1 is not one of the three outcomes, when $2 is
#         empty, or when stdin carries no id at all (no stdout in any case).
#
#   $ printf 'RoleA\nBucketB\n' | compose_retain_retry_outcome retained Stack-X
#       retry SUCCEEDED: stack Stack-X is deleted, with 2 resource(s) RETAINED.
#       ...
#         RoleA
#         BucketB
#
# ── Three outcomes, not two, and an unknown one is a hard failure ─────
#
# `rejected` (CloudFormation refused the request) and `failed` (it accepted the
# request and the stack still did not delete) are different facts and the
# operator's next move differs: a rejection means nothing was abandoned and the
# ids are still stack-managed, while a failure means the retain list was not
# sufficient to get the stack out. Collapsing them into "the retry didn't work"
# is the same class of lie this spec exists to remove — see `LGR-9e76249-8`,
# which refused to collapse "a missing record" onto "a record that establishes
# nothing" one domain over.
#
# A fourth spelling returns 2 rather than rendering. A message that says
# nothing about which of the three happened is worse than no message, because
# the operator reads it as if it did.
#
# ── An EMPTY id list returns 2, unlike `select_prefixed`'s empty result ─
#
# The two look alike and are not. `select_prefixed` selects nothing when the
# account genuinely holds nothing, so emptiness is data and success is correct
# there. Here emptiness is a caller error: there is no retain-resources retry to
# report an outcome for unless at least one resource was retained, so a
# zero-length list would render "with 0 resource(s) RETAINED" followed by an
# empty list — a statement about a retry that cannot have happened, printed in
# the middle of a real teardown. destroy.sh only calls this inside its own
# non-empty guard, so the refusal is unreachable from the one caller; it is here
# so that dropping that guard fails loudly instead of printing the lie.
#
# ── RETAINED MEANS ABANDONED, and the report says so in those words ───
#
# `delete-stack --retain-resources` "deletes the stack but doesn't delete the
# retained resources" (CloudFormation DeleteStack API reference, the
# `RetainResources` parameter). So the success arm is a stack that is gone and N
# resources that are still live, still costing money, and no longer managed by
# any template. That is a genuinely good outcome — it is the recovery the
# reporter of this defect performed by hand — but it is not the same outcome as
# a clean delete, and an operator who reads "deleted" and stops reading has
# been misled about live infrastructure. So the success arm names every
# abandoned id explicitly rather than printing a count, and says what the
# consequence is: these are exactly the fixed physical names that collide on
# the next deploy.
#
# The two non-success arms print the count only. Their ids were already listed
# in destroy.sh's hand-recovery hint immediately above, and nothing was
# abandoned on either, so re-listing them would suggest otherwise.
#
# ── The detail is printed whole, per R2.4 ─────────────────────────────
#
# Line by line at a deeper indent, never `${detail%%$'\n'*}`. A CloudFormation
# rejection routinely puts the useful clause past the first newline, and this
# text is the whole reason the operator can tell a permission problem from an
# incomplete retain list. Same posture as `compose_failure_reason`: unparsed is
# a formatting problem, dropped is a lost cause.
compose_retain_retry_outcome() {
  local outcome="${1:-}" stack="${2:-}" detail="${3:-}" line ids="" count=0
  [ -n "$stack" ] || return 2
  case "$outcome" in
    retained|rejected|failed) ;;
    *) return 2 ;;
  esac
  # stdin is consumed first and in full, so the count below is the count of the
  # list below it. The ids are accumulated already-indented, which is what lets
  # them be emitted with one `printf` and no second loop over a string.
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    ids="$ids      $line
"
    count=$(( count + 1 ))
  done
  [ "$count" -gt 0 ] || return 2
  case "$outcome" in
    retained)
      printf '    retry SUCCEEDED: stack %s is deleted, with %d resource(s) RETAINED.\n' \
        "$stack" "$count"
      printf '    RETAINED means ABANDONED, not deleted: these are still live in this account\n'
      printf '    and are no longer managed by CloudFormation. Delete them by hand, or the next\n'
      printf '    deploy collides with their fixed physical names:\n'
      printf '%s' "$ids"
      ;;
    rejected)
      printf '    retry REJECTED: CloudFormation refused the retain-resources delete of %s.\n' \
        "$stack"
      printf '    Nothing was abandoned — the %d resource(s) are still managed by the stack.\n' \
        "$count"
      ;;
    failed)
      printf '    retry FAILED: %s did not reach deletion complete with %d resource(s)\n' \
        "$stack" "$count"
      printf '    retained. Not retried again.\n'
      ;;
  esac
  # `<<<` and not a pipe: stdin is already consumed, and a pipe would put this
  # loop in a subshell for no gain. An empty detail yields one empty line here,
  # which the guard skips, so the absent case prints nothing rather than a blank.
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    printf '      %s\n' "$line"
  done <<<"$detail"
}

# compose_skipped_stack_notice — echo the report for a stack that did NOT delete
# and that destroy.sh is continuing past because the operator opted in (R2.2).
#
# $1: the deployed stack name whose deletion did not complete.
# $2: the region it was addressed in.
# $3: optional detail — CLI stderr, or the block `compose_failure_reason`
#     produced. Printed whole, line by line; an absence is stated, not blank.
# stdout: an indented report block, 4-space body and 6-space detail, matching
#         the `item`-level stream destroy.sh prints it into.
# return: 0 on success; 2 when $1 or $2 is empty (no stdout in either case).
#
#   $ compose_skipped_stack_notice Stack-X us-west-2 'DELETE_FAILED'
#       SKIPPED Stack-X (us-west-2): it did NOT delete and is STILL STANDING.
#       ...
#
# ── This is the OTHER kind of continue, and the difference is the hazard ──
#
# `destroy.sh` has two paths past a stack that reached DELETE_FAILED and they
# are not variants of each other. The retain-resources retry's `continue` fires
# only once the stack is GONE, so the dependency ordering still holds and
# nothing downstream is at risk. This one fires with the stack STILL THERE,
# which is exactly the case `destroy.sh`'s stop-on-first-failure posture was
# ratified against: "after a failed deletion the next request either fails on a
# live dependency or removes something a still-standing stack needs."
#
# So the notice does not merely say "skipped". It says what the operator is
# about to see: the stacks after this one in `ADI_STACK_DELETION_ORDER` may fail
# BECAUSE this one is still holding a dependency, and a cascade of such failures
# is ONE cause with several symptoms. An operator who reads them as several
# independent problems will chase the wrong ones — that misreading is the whole
# cost of this flag, and stating it here is what buys the flag its keep.
#
# ── Stack and region are required; the detail is not ──────────────────
#
# A notice about no particular stack, or about a stack in no particular region,
# is not a notice — the region is load-bearing here because EdgeWaf is addressed
# in us-east-1 while everything else is in the operator's region, so "Stack-X
# was skipped" is ambiguous about which request failed. Both empty cases return
# 2 rather than rendering, the same posture `compose_retain_retry_outcome` takes
# on an unknown outcome.
#
# An absent detail is different in kind and is NOT a failure. The skip happened
# whether or not a reason was obtainable — both reason lookups in destroy.sh are
# best-effort by design — so this states the absence and reports the skip,
# matching `compose_failure_reason`, where "a blank line where a reason should be
# reads as a tool that did not look". It deliberately does not match
# `compose_retain_retry_outcome`'s empty-stdin refusal, which exists because a
# zero-length retain list would describe a retry that cannot have happened. A
# reasonless skip, by contrast, certainly happened.
compose_skipped_stack_notice() {
  local stack="${1:-}" region="${2:-}" detail="${3:-}" line
  [ -n "$stack" ] || return 2
  [ -n "$region" ] || return 2
  printf '    SKIPPED %s (%s): it did NOT delete and is STILL STANDING.\n' \
    "$stack" "$region"
  printf '    Continuing because --continue-past-failed-stacks was passed. The stacks\n'
  printf '    after this one are deleted in dependency order, so their deletions may fail\n'
  printf '    BECAUSE this one still holds a dependency. Read a run of failures after this\n'
  printf '    line as ONE cause with several symptoms, not as several problems.\n'
  case "$detail" in
    "") printf '      no reason reported by CloudFormation\n' ;;
    *)
      # `<<<` and not a pipe: this function reads no stdin, and a pipe would put
      # the loop in a subshell for no gain. Printed whole, never
      # `${detail%%$'\n'*}` — R2.4's posture, and the reason block arriving here
      # from `compose_failure_reason` is multi-line by construction.
      while IFS= read -r line || [ -n "$line" ]; do
        [ -n "$line" ] || continue
        printf '      %s\n' "$line"
      done <<<"$detail"
      ;;
  esac
}

# resolve_account — echo the AWS account id for a profile (R1.1, R1.2).
#
# $1: an AWS profile name.
# stdout: the resolved 12-digit account id, on success.
# return: 0 when the id resolves and is well-formed; 1 otherwise (no stdout).
#
#   $ resolve_account "$PROFILE"
#   <the twelve digits that profile resolves to>
#
# No example account id is written even here, and that is not fussiness: an
# account-shaped literal in a comment is copyable, and being copyable is the
# entire mechanism of the incident `LGR-1166391-2` records.
#
# The only function here that touches AWS, and it is read-only:
# `sts:GetCallerIdentity` reads the caller's own identity and mutates nothing.
# It runs *before* destroy.sh reports the target and before anything is
# deleted, which is what makes R1.2 enforceable — an unresolvable account
# terminates the run with nothing touched.
#
# The result is validated as exactly 12 digits, not merely non-empty. The CLI
# can succeed while emitting something that is not an account id — `None` for
# an unresolved query, or a wrapped warning on stdout — and this value is about
# to be reported to the operator as *the account being destroyed*. A value that
# looks like an account but is not would make that report a lie at the one
# moment the operator is relying on it. `deploy-cdk/lib/resolve-env.ts` rejects
# anything that is not 12 digits for the same reason on the deploy side, so the
# pair agrees on what counts as an account.
#
# stderr is discarded because a failure is signalled by the return code and
# destroy.sh owns the operator-facing message, next to its own reporting — the
# same split `deploy_prereq_lib.sh` keeps with `fail()`.
resolve_account() {
  local profile="${1:-}" account
  [ -n "$profile" ] || return 1
  account="$(aws sts get-caller-identity \
    --profile "$profile" \
    --query Account \
    --output text 2>/dev/null)" || return 1
  case "$account" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
    *) return 1 ;;
  esac
  printf '%s\n' "$account"
}

# ── AgentCore runtime sweep (p64 R2.5, R2.6) ──────────────────────────
#
# The computations behind `scripts/cleanup_agentcore_runtimes.sh`. Both read the
# `--output text` rendering of
#
#   bedrock-agentcore-control list-agent-runtimes --query \
#     "agentRuntimes[].[agentRuntimeName,agentRuntimeId]"
#
# which is one row per runtime, `agentRuntimeName<TAB>agentRuntimeId`. Same
# two-field text projection destroy.sh already relies on for its policy
# enumeration and for the DELETE_FAILED events, so this adds no new assumption
# about the CLI's output format.
#
# ── THREE identifier spaces are in play, and delete takes the second ──
#
# For one runtime, confirmed against live state in the fresh-account (task 0.3):
#
#   agentRuntimeName    agentic_data_ingestion_agent_create_code
#   agentRuntimeId      agentic_data_ingestion_agent_create_code-1cxjt261tA
#   LogicalResourceId   CreateCodeAgentRuntime
#
# `delete-agent-runtime --agent-runtime-id` takes the SECOND. The id is the name
# plus `-` plus a ten-character suffix, so it looks derivable from the name — and
# it MUST NOT be derived. That suffix is assigned by the AgentCore control plane,
# it is not in this repo, and it is not reproducible from anything that is: a
# composed id is a guess, and a guessed id passed to a delete either errors or
# names a different runtime. So the id only ever travels as the second field of
# the row the API itself returned, paired with the name it arrived beside. That
# pairing is why `select_agentcore_runtimes` emits BOTH fields rather than
# selecting on names and looking ids up afterwards — a second lookup is a second
# chance to pair a name with the wrong id.
#
# Nothing below encodes the `<name>-<10 chars>` shape as a *requirement* either.
# Validating against it would bake the same derivation in through the back door
# and would refuse a legitimate id the day the control plane changes its suffix.
# The only test applied is the character grammar, which is what keeps a malformed
# argument out of the delete.

# _looks_like_agentcore_runtime_id — is $1 usable as an --agent-runtime-id?
#
# Internal to the two functions below, and the sibling of
# `_looks_like_logical_id`. Same posture and same reason: this value is bound for
# a delete request, so the test excludes rather than interprets.
#
# `None` is excluded explicitly even though it is alphanumeric, because
# `aws --output text` prints `None` for a null field — the spelling destroy.sh
# already guards every other text-mode list against.
#
# The grammar is alphanumerics plus `_` and `-`, which is wider than
# `_looks_like_logical_id`'s alphanumerics-only because a runtime id genuinely
# contains both: the name half carries underscores and the suffix is joined with
# a hyphen. It is still narrow enough to exclude every field separator, so a row
# carrying a THIRD field cannot reach a delete — the extra tab lands inside this
# candidate and fails the class test. That is a deliberate consequence, not a
# coincidence: the query above pins two fields, and a row with more of them is a
# row this library does not understand.
_looks_like_agentcore_runtime_id() {
  local candidate="${1:-}"
  [ -n "$candidate" ] || return 1
  [ "$candidate" != "None" ] || return 1
  case "$candidate" in
    *[!A-Za-z0-9_-]*) return 1 ;;
  esac
  return 0
}

# select_agentcore_runtimes — echo the rows whose NAME begins with $1 (R2.5).
#
# $1: the required prefix, matched literally and anchored at the start of the
#     runtime NAME. Never against the id.
# stdin: `agentRuntimeName<TAB>agentRuntimeId` rows, as rendered above.
# stdout: the selected rows, unchanged and in input order, one per line.
# return: 0 on success (including when nothing is selected); 2 when $1 is empty
#         (no stdout).
#
#   $ printf 'agentic_data_ingestion_agent_create_code\tagentic_data_ingestion_agent_create_code-1cxjt261tA\nsomeone_elses_runtime\tsomeone_elses_runtime-AAAAAAAAAA\n' \
#       | select_agentcore_runtimes agentic_data_ingestion_
#   agentic_data_ingestion_agent_create_code	agentic_data_ingestion_agent_create_code-1cxjt261tA
#
# ── This function's EXCLUSIONS are the safety property ────────────────
#
# Everything selected here is a deletion candidate for an AI agent runtime and
# there is no undo, so the posture is `failed_resource_ids`' and not
# `select_prefixed`'s: UNDER-select when in doubt. A row is selected only when
# all of these hold, and a row failing any of them is dropped in silence because
# dropping it is the safe outcome:
#
#   * it has a tab, so it is a two-field row rather than a wrapped fragment;
#   * its first field carries the prefix literally, tested with
#     `${name#"$prefix"}` and not `grep "^$prefix"` or `case "$name" in
#     "$prefix"*)` — both of which read the prefix as a pattern, so a prefix
#     containing `.`, `*`, `?` or `[` would match names it should not. The
#     quoted expansion suppresses pattern interpretation, making this an exact
#     literal-prefix test for any prefix whatsoever. Same technique
#     `select_prefixed` documents at length;
#   * its second field passes the id grammar.
#
# An empty prefix returns 2 rather than selecting nothing, closing the same two
# ways to over-select `select_prefixed` closes: a caller whose prefix variable
# came out empty gets a loud failure instead of a silently empty enumeration
# reading as "nothing to sweep", and an empty prefix cannot match everything.
#
# ── WHICH prefix, because the difference is five live runtimes ────────
#
# `LGR-9932c9b-1` (template / ratified) rejected a prefix match for the Glue
# sweep because a second naming scheme shared the prefix, and "where the
# read-only exclusion would merely hide a real target, a sweep would DELETE
# one". That caution transfers here and the numbers are not close, measured
# against live state (task 0.3):
#
#   agentic_data_ingestion_        matches ALL 14 runtimes
#   agentic_data_ingestion_agent_  matches 9 — the agent runtimes only
#
# The other five are the MCP server runtimes. `docs/ARCHITECTURE.md` § System
# Inventory is the authority for the split and states it as
# `AgentRuntimeName: entries | 14 = 5 + 9`; the "nine" in p64's own design C4 is
# the separate `agent containers | 9` figure. Both numbers are right about
# different sets, which is exactly why a tool in this namespace has to say which
# one it means.
#
# The caller decides and the prefix is an argument, for the same reason
# `select_prefixed` takes one. What the caller SHOULD default to, and why, is
# argued at `cleanup_agentcore_runtimes.sh`'s `DEFAULT_RUNTIME_PREFIX` — the
# short form being that all 14 names are hardcoded without the `${environment}`
# suffix, so all 14 collide on the next deploy and a sweep covering 9 of them
# leaves the tool failing its own purpose for the other 5.
#
# What makes the wider prefix defensible here and not there is that the Glue
# case's second scheme has no analogue: `AgentRuntimeName` appears 14 times in
# `deploy-cdk/lib/` and every one is in `agentcore-stack.ts`, and no
# `create_agent_runtime` call exists anywhere in `lambda/`, `shared/`, `mcp/`,
# `agentcore/`, `scripts/` or `tests/` — verified by grep, not assumed. So
# nothing creates a runtime at request time the way
# `create_glue_pipeline/iam_roles.py` creates per-pipeline Glue databases. One
# producer owns this namespace.
#
# That argument is about THIS repo and cannot speak for a shared account, so it
# is not the only guard. The driver's real discriminator is whether the
# AgentCore CloudFormation stack still exists — see
# `compose_agentcore_sweep_refusal`, which refuses on evidence about the
# resources rather than on inference about their names.
#
# `|| [ -n "$line" ]` catches a final line with no trailing newline, which
# `read` reports as failure after having populated `line`.
select_agentcore_runtimes() {
  local prefix="${1:-}" line name id
  [ -n "$prefix" ] || return 2
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    name="${line%%$'\t'*}"
    # Equal means no tab was found, so this is not a runtime row.
    [ "$name" != "$line" ] || continue
    [ "${name#"$prefix"}" != "$name" ] || continue
    id="${line#*$'\t'}"
    _looks_like_agentcore_runtime_id "$id" || continue
    printf '%s\t%s\n' "$name" "$id"
  done
}

# compose_agentcore_sweep_refusal — echo the report for a sweep that will NOT
# delete, because the AgentCore stack is still there (R2.5).
#
# $1: the deployed AgentCore stack name that was looked for.
# $2: the region it was looked for in.
# $3: optional `StackStatus`, as `--output text` rendered it (`None` and empty
#     are both accepted and reported as an absence).
# stdout: an indented report block, 4-space body and 6-space detail.
# return: 0 on success; 2 when $1 or $2 is empty (no stdout).
#
#   $ compose_agentcore_sweep_refusal AgenticDataIngestion-AgentCore-dev us-west-2 UPDATE_COMPLETE
#       REFUSING to delete: stack AgenticDataIngestion-AgentCore-dev still exists in us-west-2.
#       ...
#
# ── The precondition, and why it is the right one ─────────────────────
#
# Every runtime this tool can see is declared in `agentcore-stack.ts`, so while
# that stack exists the runtimes are LIVE, stack-managed resources and not
# orphans. Deleting one then is strictly worse than leaving it: the template
# still claims a resource that is gone, so the next `cdk deploy` or the next
# `cdk destroy` operates on a stack whose real state it does not know, and the
# runtime the system needs is missing meanwhile.
#
# The orphan this tool exists for arises in exactly one shape, and it is the
# shape destroy.sh produces: the `--retain-resources` retry (p64 R2.1) deletes
# the STACK while ABANDONING the resources that would not go. Stack gone,
# runtimes live, nothing managing them, fixed physical names still occupied.
# `compose_retain_retry_outcome` names that outcome in those words — "these are
# still live in this account and are no longer managed by CloudFormation" — and
# this helper is the tool for the AgentCore ones.
#
# So the refusal is evidence about the resources, not inference about their
# names, and it is what makes the wide default prefix safe: at the moment a
# delete is permitted, the stack that produced all 14 names is gone.
#
# ── This is ALSO the standalone justification (p64 finding 1) ─────────
#
# p64's design cites `LGR-9932c9b-1` for not wiring this into destroy.sh, and
# for AgentCore that citation does not hold. The record's reason was that
# destroy.sh "deletes no Glue resources at all, so bolting on an e2e-specific
# tool would imply the catalog is handled when it is not". destroy.sh DOES
# delete the AgentCore stack — `AgentCore` is position 6 of 9 in
# `ADI_STACK_DELETION_ORDER` — and these runtimes are resources of it. A
# leftover runtime is an orphan of a delete destroy.sh attempted, not of a
# resource class it ignores.
#
# The helper stays standalone on a different and better argument: its
# precondition is a state destroy.sh cannot be in the middle of. At every point
# inside destroy.sh's AgentCore step the stack either still exists — the delete
# failed, or the retry has not run yet — in which case this sweep must refuse,
# or it is gone and destroy.sh has already moved on to the next stack in
# dependency order. A call sited inside that step would therefore either refuse
# by construction or fire while the operator is being told the stack was
# retained. Both are worse than a separate tool the operator runs afterwards,
# having read what was abandoned.
#
# The record's OTHER half does transfer and is honoured — see
# `select_agentcore_runtimes` for the prefix argument.
compose_agentcore_sweep_refusal() {
  local stack="${1:-}" region="${2:-}" status="${3:-}"
  [ -n "$stack" ] || return 2
  [ -n "$region" ] || return 2
  printf '    REFUSING to delete: stack %s still EXISTS in %s.\n' "$stack" "$region"
  case "$status" in
    ""|None) printf '      CloudFormation reported no status for it.\n' ;;
    *) printf '      CloudFormation reports its status as %s.\n' "$status" ;;
  esac
  printf '    Every runtime this tool can see is declared in that stack, so while the stack\n'
  printf '    stands they are LIVE, stack-managed resources — not orphans. Deleting one now\n'
  printf '    leaves the template claiming a resource that is gone, and the system missing a\n'
  printf '    runtime it needs.\n'
  printf '    Tear the stack down first (./destroy.sh). Come back to this tool only for the\n'
  printf '    runtimes a --retain-resources retry ABANDONED, which is the one case that\n'
  printf '    leaves a runtime live with no stack managing it.\n'
}
