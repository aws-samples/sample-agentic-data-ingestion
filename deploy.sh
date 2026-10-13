#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# deploy.sh — one-command deployment for Agentic Data Ingestion.
#
# Validates prerequisites, resolves AWS account/region from your credentials,
# installs CDK dependencies, bootstraps CDK in the app region AND us-east-1
# (required by the CLOUDFRONT-scope EdgeWaf stack), and deploys all stacks.
# The React dashboard is built automatically during deploy via CDK asset
# bundling — no manual npm build step.
#
# Usage:
#   ./deploy.sh [--dry-run|--check] [--environment <name>]
#
#   --dry-run, --check   Run only the prerequisite checks, then exit.
#   --environment <name> Deploy a named environment (default: dev).
#
# Tunables (environment variables):
#   ADI_MIN_DISK_GB      Hard minimum free disk in GB (default: 50).
#                        100 GB is recommended for a first full deploy.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDK_DIR="$SCRIPT_DIR/deploy-cdk"
MIN_DISK_GB="${ADI_MIN_DISK_GB:-50}"
RECOMMENDED_DISK_GB=100

# Pure prerequisite helpers live in a sourceable library so they can be
# exercised directly by unit tests instead of only through a full sweep.
# shellcheck source=scripts/deploy_prereq_lib.sh
. "$SCRIPT_DIR/scripts/deploy_prereq_lib.sh"

DRY_RUN=0
ENVIRONMENT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|--check) DRY_RUN=1 ;;
    --environment)
      shift
      [ $# -gt 0 ] || { echo "error: --environment requires a value" >&2; exit 2; }
      ENVIRONMENT="$1"
      ;;
    -h|--help)
      # Print the header block: skip the shebang, then emit comment lines with
      # their marker stripped and stop at the first line that is not a comment.
      # Self-delimiting on purpose — the previous form ended at a hardcoded
      # line 19, one past the block, so `set -euo pipefail` was printed as if it
      # were documentation. Any end line number here goes stale the next time a
      # header line is added, and silently, since --help is asserted nowhere.
      awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) echo "error: unknown argument '$1' (see --help)" >&2; exit 2 ;;
  esac
  shift
done

# ── Prerequisite checks (R8.2/8.3) ────────────────────────────────────
# All checks run; every failure is reported with its fix, then exit 1.
FAILURES=()
WARNINGS=()

fail() { FAILURES+=("$1"); }
warn() { WARNINGS+=("$1"); }

check_node() {
  if ! command -v node >/dev/null 2>&1; then
    fail "Node.js not found — install Node 20+ (https://nodejs.org or nvm)"
    return
  fi
  local major
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  if [ "$major" -lt 20 ]; then
    fail "Node.js $(node --version) is too old — this project requires Node 20+ (nvm install 20)"
  fi
}

check_python() {
  # Probe the candidate list (python3, then python3.12) and keep the resolved
  # interpreter: on Amazon Linux 2023, `dnf install python3.12` leaves python3
  # at 3.9, so the requirement can be satisfied under a non-default name.
  local resolved
  if resolved="$(resolve_python_interpreter)"; then
    RESOLVED_PYTHON="$resolved"
    echo "    Python: using $RESOLVED_PYTHON ($("$RESOLVED_PYTHON" --version 2>&1 | awk '{print $2}'))"
    return
  fi
  # No candidate reported 3.12+. Name what was found and what to install, and
  # deliberately do NOT suggest re-pointing the python3 symlink: on AL2023 that
  # link is managed by the system package manager and dnf itself runs on the
  # system Python, so repointing it can break package management on the host
  # (docs/DEPLOY_EC2.md).
  local found=() candidate version
  for candidate in "${ADI_PYTHON_CANDIDATES[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1; then
      version="$("$candidate" --version 2>&1 | awk '{print $2}')"
      found+=("$candidate is ${version:-an unreadable version}")
    else
      found+=("$candidate not found")
    fi
  done
  local found_list
  found_list="$(printf '%s, ' "${found[@]}")"
  fail "no Python 3.12+ interpreter found (${found_list%, }) — install Python 3.12+ (Amazon Linux 2023: sudo dnf install -y python3.12). deploy.sh probes python3.12 directly, so do NOT re-point the python3 symlink (see docs/DEPLOY_EC2.md)"
}

# classify_docker_failure — name the cause of a failed `docker info`.
#
# $1: the stderr text `docker info` produced.
# stdout: `permission`, `daemon`, or `unknown`.
#
# The two causes have different fixes (group membership + a fresh session vs
# starting the daemon), so they cannot share one message. `docker info`'s exit
# status says nothing about which it is; its stderr does — a socket permission
# error names "permission denied" explicitly, and an unreachable daemon says
# "Cannot connect to the Docker daemon". Permission is tested first, because
# some Docker versions mention both in one message.
#
# When the text is unrecognised, fall back to the same second probe
# docs/DEPLOY_EC2.md tells the reader to use — `systemctl is-active docker`
# answers "is the daemon running", `docker info` answers "can I reach it" —
# but only where systemctl exists. macOS/Docker Desktop has no systemctl, so it
# must not become a hard dependency of this script.
classify_docker_failure() {
  local err="$1"
  case "$err" in
    *"permission denied"*|*"Permission denied"*) echo permission; return ;;
  esac
  case "$err" in
    *"Cannot connect to the Docker daemon"*|*"Is the docker daemon running"*|\
    *"no such file or directory"*|*"connection refused"*) echo daemon; return ;;
  esac
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet docker 2>/dev/null; then
      echo unknown   # daemon is up, so this is neither of the two known causes
    else
      echo daemon
    fi
    return
  fi
  echo unknown
}

check_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    fail "docker not found — install Docker (https://docs.docker.com/get-docker/)"
    return
  fi
  local docker_err first_line
  if ! docker_err="$(docker info 2>&1 >/dev/null)"; then
    case "$(classify_docker_failure "$docker_err")" in
      permission)
        fail "Docker socket permission denied — the daemon is there, this session cannot reach /var/run/docker.sock. Add yourself to the docker group and open a FRESH session: sudo usermod -aG docker \"\$USER\", then exit and reconnect (for SSM, end the session and start a new one). Group membership is fixed when a session is created, so nothing you run inside this one applies it. Do NOT chmod the socket: mode 666 makes it world-writable, and anything that can talk to the daemon can mount the host filesystem in a privileged container — root-equivalent host access for every local user (docs/TROUBLESHOOTING.md)"
        ;;
      daemon)
        fail "Docker is installed but the daemon is not running — start it and enable it at boot: sudo systemctl enable --now docker (on Amazon Linux 2023 'dnf install docker' does neither), or launch Docker Desktop on macOS/Windows. Confirm with: systemctl is-active docker (see docs/DEPLOY_EC2.md)"
        ;;
      *)
        first_line="${docker_err%%$'\n'*}"
        [ -n "$first_line" ] || first_line="docker info failed with no error output"
        fail "Docker daemon not reachable: ${first_line} — check the daemon is running (systemctl is-active docker on Linux, or Docker Desktop's status) and that this session can reach the socket (docker info, without sudo). A permission error on /var/run/docker.sock is fixed by the docker group plus a fresh session; do NOT chmod the socket (docs/TROUBLESHOOTING.md)"
        ;;
    esac
    return
  fi
  # ARM64 build capability: native ARM host, or buildx with linux/arm64 support.
  local arch
  arch="$(uname -m)"
  if [ "$arch" != "arm64" ] && [ "$arch" != "aarch64" ]; then
    if ! docker buildx ls 2>/dev/null | grep -q 'linux/arm64'; then
      fail "Docker cannot build linux/arm64 images on this $arch host — enable QEMU/binfmt: docker run --privileged --rm tonistiigi/binfmt --install arm64 (see docs/TROUBLESHOOTING.md)"
    fi
  fi
}

check_disk() {
  # Measure free space where Docker stores data when that path exists on this
  # host (Linux); on Docker Desktop (macOS/Windows) the root dir lives inside
  # the VM, so fall back to the project filesystem as a proxy.
  local probe_path free_kb free_gb docker_root
  probe_path="$SCRIPT_DIR"
  if docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" \
     && [ -n "$docker_root" ] && [ -d "$docker_root" ]; then
    probe_path="$docker_root"
  fi
  free_kb="$(df -Pk "$probe_path" | awk 'NR==2 {print $4}')"
  free_gb=$((free_kb / 1024 / 1024))
  if [ "$free_gb" -lt "$MIN_DISK_GB" ]; then
    fail "Only ${free_gb} GB free on $probe_path — need at least ${MIN_DISK_GB} GB (${RECOMMENDED_DISK_GB} GB recommended). Free space or grow the volume; docker system prune -af reclaims image cache"
  elif [ "$free_gb" -lt "$RECOMMENDED_DISK_GB" ]; then
    warn "${free_gb} GB free on $probe_path — a first full deploy is happier with ${RECOMMENDED_DISK_GB} GB (Docker image builds + layer bundling)"
  fi
}

check_aws() {
  if ! command -v aws >/dev/null 2>&1; then
    fail "AWS CLI not found — install AWS CLI v2 (https://docs.aws.amazon.com/cli/)"
    return
  fi
  local account
  if ! account="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)"; then
    fail "AWS credentials not resolvable — configure credentials (aws configure / SSO / env vars) and retry: aws sts get-caller-identity"
    return
  fi
  if ! printf '%s' "$account" | grep -Eq '^[0-9]{12}$'; then
    fail "aws sts get-caller-identity returned '$account', not a 12-digit account ID — check your credentials"
    return
  fi
  RESOLVED_ACCOUNT="$account"
}

echo "==> Checking prerequisites"
RESOLVED_ACCOUNT=""
RESOLVED_PYTHON=""
check_node
check_python
check_docker
check_disk
check_aws

if [ ${#WARNINGS[@]} -gt 0 ]; then
  for w in "${WARNINGS[@]}"; do echo "WARN: $w"; done
fi

if [ ${#FAILURES[@]} -gt 0 ]; then
  echo ""
  echo "Prerequisite check FAILED (${#FAILURES[@]} issue(s)):"
  for f in "${FAILURES[@]}"; do echo "  ✗ $f"; done
  echo ""
  echo "Fix the issues above and re-run. (./deploy.sh --dry-run re-checks without deploying.)"
  exit 1
fi
echo "All prerequisite checks passed."

if [ "$DRY_RUN" -eq 1 ]; then
  echo "--dry-run: stopping after prerequisite checks."
  exit 0
fi

# ── Environment resolution ────────────────────────────────────────────
export CDK_DEFAULT_ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$RESOLVED_ACCOUNT}"

# The interpreter check_python resolved, for anything downstream that needs
# host Python — a bare `python3` is 3.9 on Amazon Linux 2023. The deploy path
# below invokes Python only inside the SAM build container (compute-stack.ts
# layer bundling), so nothing shells out to the host interpreter today; the
# export is what keeps the resolved one authoritative when something does.
export ADI_PYTHON="$RESOLVED_PYTHON"

REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-}}"
if [ -z "$REGION" ]; then
  REGION="$(aws configure get region 2>/dev/null || true)"
fi
if [ -z "$REGION" ]; then
  REGION="us-west-2"
  echo "NOTE: no region configured — defaulting to $REGION (set CDK_DEFAULT_REGION to override)"
fi
export CDK_DEFAULT_REGION="$REGION"

echo "==> Deploying to account $CDK_DEFAULT_ACCOUNT, region $CDK_DEFAULT_REGION${ENVIRONMENT:+, environment $ENVIRONMENT}"

# ── Install CDK dependencies (R8.4) ───────────────────────────────────
# The dashboard is NOT built here: the Hosting stack builds it automatically
# during deploy via CDK asset bundling.
echo "==> Installing CDK dependencies (npm ci)"
npm ci --prefix "$CDK_DIR"

# ── Prune orphaned asset staging from cdk.out ─────────────────────────
# Deliberately a PRUNE, not a purge: cdk.out is the asset cache that lets an
# unchanged container image or Lambda layer be reused instead of rebuilt, so
# clearing it wholesale would make this deploy slower, not faster. Only assets
# the current manifest no longer references are removed. See the header of
# scripts/prune_cdk_out.sh for why they matter: aws-cdk-lib re-hashes the whole
# of cdk.out twice per synth while a validation plugin is attached, so orphaned
# assets are pure cost on every subsequent synth.
#
# Runs before bootstrap so the saving lands on the bootstrap synth too. It is
# advisory, never fatal: the script exits 0 on every refusal, and the || guard
# covers the case where it is missing or not executable so a housekeeping step
# can never be the reason a deploy does not start.
echo "==> Pruning orphaned CDK asset staging"
"$SCRIPT_DIR/scripts/prune_cdk_out.sh" "$CDK_DIR/cdk.out" \
  || echo "WARN: cdk.out prune did not run (non-fatal — continuing)"

# ── Bootstrap both regions, idempotently (R8.5) ───────────────────────
# us-east-1 is always required: the EdgeWaf stack (CLOUDFRONT-scope WAF)
# deploys there and the Hosting stack consumes its ARN cross-region.
#
# The target list comes from bootstrap_targets rather than being spelled out
# here, because the two are the same environment when the app region IS
# us-east-1 — and naming it twice in one invocation made CDK race two change
# sets against the same bootstrap stack. Read into an array so each target
# stays one argument; do not collapse this to an unquoted expansion.
BOOTSTRAP_TARGETS=()
while IFS= read -r bootstrap_target; do
  BOOTSTRAP_TARGETS+=("$bootstrap_target")
done < <(bootstrap_targets "$CDK_DEFAULT_ACCOUNT" "$CDK_DEFAULT_REGION")
echo "==> Bootstrapping CDK (${#BOOTSTRAP_TARGETS[@]} environment(s): ${BOOTSTRAP_TARGETS[*]})"
(
  cd "$CDK_DIR"
  # -c adi:skipNag=true applies to THIS INVOCATION ONLY, and is a cost removal
  # rather than a posture change. `cdk bootstrap` still runs the app (cdk.json's
  # app command), so it pays for cdk-nag's AwsSolutionsChecks plugin in full —
  # including the two whole-of-cdk.out hash snapshots aws-cdk-lib takes around
  # every validation plugin — while auditing nothing: bootstrap deploys CDK's
  # own bootstrap template, not this app's stacks, so no finding it produced
  # could ever be actioned.
  #
  # The deploy invocation below deliberately does NOT pass this flag, and the
  # default in bin/agentic_data_ingestion.ts:24 is unchanged, so nag remains
  # fully active for `cdk deploy` and for `cdk synth`. Do not "tidy" this by
  # hoisting the flag to a shared variable — the whole point is that exactly one
  # of the two invocations carries it.
  npx cdk bootstrap -c adi:skipNag=true "${BOOTSTRAP_TARGETS[@]}"
)

# ── Deploy (R8.6) ─────────────────────────────────────────────────────
# Concurrency: deploy independent stacks in parallel instead of strictly one at
# a time. Foundation, Data, Auth and EdgeWaf declare no dependency on each other
# (bin/agentic_data_ingestion.ts:127-224), so serialising them buys nothing. CDK
# honours the dependency graph regardless of this value — AgentCore still waits
# for Foundation, Compute for all four of its parents, Hosting for Compute/Auth/
# EdgeWaf — so this cannot reorder a deploy into an invalid sequence.
#
# 2, not 4, and not "however many independent stacks there are": a concurrent
# stack can be publishing container image assets, which means a Docker build per
# image, and this repo builds 19 of them (linux/arm64). On a memory-constrained
# host, N parallel Docker builds plus an 8 GB-ceilinged ts-node synth is how you
# turn a slow deploy into a swapping one — at which point parallelism is
# negative. 2 takes the easy win on the independent leading edge of the graph
# while keeping peak memory and disk I/O close to today's.
#
# ADI_DEPLOY_CONCURRENCY overrides it without editing this file; set it to 0 (or
# 1) to get exactly today's serial behaviour back if a host cannot take even 2.
DEPLOY_CONCURRENCY="${ADI_DEPLOY_CONCURRENCY:-2}"

echo "==> Deploying all stacks (this can take 10-30 minutes on first deploy)"
CDK_ARGS=(deploy)
if [ -n "$ENVIRONMENT" ]; then
  # Context flags must precede any positional arguments.
  CDK_ARGS+=(-c "environment=$ENVIRONMENT")
fi
CDK_ARGS+=(--all --require-approval never)
# Appended, rather than collected into a possibly-empty array and expanded:
# macOS ships bash 3.2, where `"${empty[@]}"` under `set -u` is a fatal
# "unbound variable" — so the opt-out would have broken the very deploy it
# exists to rescue. Verified: bash 3.2.57 on this host errors on that form.
case "$DEPLOY_CONCURRENCY" in
  0|1) ;;   # omit the flag entirely: exactly the pre-change serial behaviour
  *) CDK_ARGS+=(--concurrency "$DEPLOY_CONCURRENCY") ;;
esac
cd "$CDK_DIR"
DEPLOY_STATUS=0
npx cdk "${CDK_ARGS[@]}" || DEPLOY_STATUS=$?

# ── Post-deploy stack completeness check ──────────────────────────────
# An interrupted `cdk deploy --all` can leave sibling stacks undeployed
# (e.g. no Orchestration stack -> pipelines silently never execute). Verify
# every expected stack exists and is healthy, whatever cdk exited with.
#
# Two loops because the app deploys to two regions. ADI_APP_REGION_STACKS is
# checked in $CDK_DEFAULT_REGION; ADI_EDGE_REGION_STACKS is checked in
# us-east-1, where the EdgeWaf stack's CLOUDFRONT-scope WAF must live. Both feed
# the same MISSING flag and the same exit path, so the two together are the
# whole expectation. TestFixtures is context-gated on
# `-c adi:deployTestFixtures=true`, so a plain --all deploy does not create it
# and it is correctly in neither list.
#
# do NOT "tidy" this into one loop by appending EdgeWaf to
# ADI_APP_REGION_STACKS: it would be queried in the app region, where it does
# not exist, so a correct deploy would report a missing stack and exit 1.
#
# The sets, the name construction and the status classification are in
# scripts/deploy_prereq_lib.sh, where tests can reach them — the describe-stacks
# calls stay here because the I/O is not the part that goes wrong quietly.
ENV_NAME="${ENVIRONMENT:-dev}"
echo "==> Verifying expected stacks"
MISSING=0
for s in "${ADI_APP_REGION_STACKS[@]}"; do
  STACK="$(stack_name "$s" "$ENV_NAME")"
  STATUS="$(aws cloudformation describe-stacks --stack-name "$STACK" \
    --region "$CDK_DEFAULT_REGION" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "$ADI_STACK_ABSENT")"
  case "$(classify_stack_status "$STATUS")" in
    ok) echo "    ok      $STACK ($STATUS)" ;;
    missing) echo "    MISSING $STACK — pipelines will not work; re-run ./deploy.sh"; MISSING=1 ;;
    *) echo "    WARN    $STACK is in state $STATUS"; MISSING=1 ;;
  esac
done
# EdgeWaf lives in us-east-1 regardless of the app region — hence a second loop
# against a different region rather than another entry in the array above.
for s in "${ADI_EDGE_REGION_STACKS[@]}"; do
  STACK="$(stack_name "$s" "$ENV_NAME")"
  STATUS="$(aws cloudformation describe-stacks --stack-name "$STACK" \
    --region us-east-1 \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "$ADI_STACK_ABSENT")"
  case "$(classify_stack_status "$STATUS")" in
    ok) echo "    ok      $STACK ($STATUS, us-east-1)" ;;
    missing) echo "    MISSING $STACK (us-east-1)"; MISSING=1 ;;
    *) echo "    WARN    $STACK is in state $STATUS (us-east-1)"; MISSING=1 ;;
  esac
done

if [ "$MISSING" -ne 0 ]; then
  echo ""
  echo "Deployment is INCOMPLETE — one or more stacks are missing or unhealthy (see above)."
  exit 1
fi
# Counted from the two arrays rather than written out, for the same reason
# --help no longer ends at a hardcoded line number: a literal count is a second
# place to edit, and the one that goes stale silently.
echo "All $(( ${#ADI_APP_REGION_STACKS[@]} + ${#ADI_EDGE_REGION_STACKS[@]} )) stacks present and healthy."
exit "$DEPLOY_STATUS"
