#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# prune_cdk_out.sh — remove ORPHANED asset staging directories from cdk.out.
#
# Usage:
#   scripts/prune_cdk_out.sh [<cdk.out path>]     # default: deploy-cdk/cdk.out
#
# Why this exists, and why it is a prune and not a purge
# ------------------------------------------------------
# `cdk.out` is CDK's asset staging area, and it is a CACHE: an asset directory
# named `asset.<sha256>` is reused on the next synth/deploy when its content
# hash is unchanged. Deleting `cdk.out` wholesale therefore makes deploys
# SLOWER — it forces all 19 container images and all 3 Lambda layers (each a
# pip install inside a SAM build container) to rebuild from scratch.
#
# What genuinely costs time is the assets nothing references any more. They
# accumulate one generation per content change, and aws-cdk-lib re-hashes the
# WHOLE of `cdk.out` twice per synth when a validation plugin is attached
# (`doInvokeValidationPlugins` wraps each plugin in `snapshotFileHashes(outdir)`
# to detect plugin tampering). With 1,068 accumulated `asset.*` directories —
# mostly full Python dependency trees — that turned into ~30 minutes of
# synchronous SHA-256 over files no deploy would ever read, and ended in a
# spurious validation failure.
#
# So: keep every asset the current manifest references, delete only the rest.
#
# Safety rules this script holds to
# ---------------------------------
#   * No manifest.json, or a manifest that will not parse  -> DO NOTHING, exit 0.
#     A missing manifest means "we cannot tell what is live", and the only safe
#     answer to that is to leave the tree alone. Never delete on a guess.
#   * A referenced `*.assets.json` that is missing or unreadable -> DO NOTHING,
#     exit 0. A partial keep-set would look like a successful prune while
#     deleting live assets.
#   * A synth holding a live `synth.lock` -> DO NOTHING, exit 0. The manifest
#     may be mid-write, so the keep-set would be incomplete.
#   * The delete candidates are ONLY top-level entries whose basename matches
#     `asset.*`. manifest.json, tree.json, cdk.out/cdk.out, *.template.json,
#     *.assets.json, synth.lock and error.txt are never candidates, so no
#     future edit to the keep-set logic can put them at risk.
#   * Idempotent: a second run on the same tree removes nothing.
#
# Exit status is 0 on every handled path, including every refusal, so wiring
# this into deploy.sh cannot fail a deploy.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDK_OUT="${1:-$SCRIPT_DIR/../deploy-cdk/cdk.out}"

if [ ! -d "$CDK_OUT" ]; then
  echo "    cdk.out prune: $CDK_OUT does not exist — nothing to do."
  exit 0
fi
CDK_OUT="$(cd "$CDK_OUT" && pwd)"

# ── Refuse while a synth is in flight ─────────────────────────────────
# `synth.lock` holds the PID of the synthesising process. It is NOT always
# cleaned up — a killed synth leaves a stale lock behind — so existence alone
# proves nothing and `kill -0` is what distinguishes live from stale.
LOCK="$CDK_OUT/synth.lock"
if [ -f "$LOCK" ]; then
  lock_pid="$(tr -cd '0-9' < "$LOCK")"
  if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
    echo "    cdk.out prune: a synth is running (synth.lock pid $lock_pid) — skipping."
    exit 0
  fi
fi

# ── Build the keep-set from the manifest ──────────────────────────────
# Node rather than a grep over the JSON: parsing is what makes "unparseable"
# detectable, and this script's whole safety story rests on telling
# "no referenced assets" apart from "could not read the reference list".
# Node is already a hard prerequisite of this repo's deploy path (deploy.sh
# check_node requires 20+), and cdk.out only exists because node ran.
#
# Exit codes from the helper:  0 keep-set on stdout · 3 no/bad manifest
#                              4 a referenced assets.json is missing/bad
if ! command -v node >/dev/null 2>&1; then
  echo "    cdk.out prune: node not found — skipping (cannot read manifest.json safely)."
  exit 0
fi

KEEP_LIST=""
KEEP_STATUS=0
KEEP_LIST="$(node -e '
const fs = require("fs"), path = require("path");
const out = process.argv[1];
let manifest;
try {
  manifest = JSON.parse(fs.readFileSync(path.join(out, "manifest.json"), "utf8"));
} catch (e) {
  process.exit(3);
}
// Keep the TOP-LEVEL path component of every reference, so that a reference
// to "asset.<hash>/inner/file" keeps the whole "asset.<hash>" directory.
const keep = new Set();
const add = (p) => {
  if (typeof p !== "string" || p === "") return;
  const top = p.split("/")[0];
  if (top !== "" && top !== "." && top !== "..") keep.add(top);
};
const unreadable = [];
for (const artifact of Object.values((manifest && manifest.artifacts) || {})) {
  const props = (artifact && artifact.properties) || {};
  add(props.templateFile);
  if (typeof props.file !== "string") continue;
  add(props.file);
  if (artifact.type !== "cdk:asset-manifest") continue;
  let assets;
  try {
    assets = JSON.parse(fs.readFileSync(path.join(out, props.file), "utf8"));
  } catch (e) {
    unreadable.push(props.file);
    continue;
  }
  // File assets carry source.path; container images carry source.directory.
  for (const f of Object.values(assets.files || {})) add(f && f.source && f.source.path);
  for (const d of Object.values(assets.dockerImages || {})) add(d && d.source && d.source.directory);
}
if (unreadable.length > 0) {
  console.error(unreadable.join(", "));
  process.exit(4);
}
for (const k of keep) if (k.startsWith("asset.")) console.log(k);
' "$CDK_OUT" 2>/tmp/adi_prune_err.$$)" || KEEP_STATUS=$?

KEEP_ERR="$(cat "/tmp/adi_prune_err.$$" 2>/dev/null || true)"
rm -f "/tmp/adi_prune_err.$$"

case "$KEEP_STATUS" in
  0) ;;
  3)
    echo "    cdk.out prune: no readable manifest.json in $CDK_OUT — skipping (nothing is deleted without one)."
    exit 0
    ;;
  4)
    echo "    cdk.out prune: referenced asset manifest(s) unreadable (${KEEP_ERR:-unknown}) — skipping rather than prune on a partial list."
    exit 0
    ;;
  *)
    echo "    cdk.out prune: could not read the manifest (status $KEEP_STATUS${KEEP_ERR:+: $KEEP_ERR}) — skipping."
    exit 0
    ;;
esac

# ── Remove the asset.* entries the keep-set does not name ─────────────
removed=0
reclaimed_kb=0
for candidate in "$CDK_OUT"/asset.*; do
  # An unmatched glob yields the literal pattern; -e filters it out.
  [ -e "$candidate" ] || continue
  name="$(basename "$candidate")"
  # Belt and braces: the loop is already rooted at $CDK_OUT/asset.*, and this
  # re-asserts the only thing that makes a path deletable at all.
  case "$name" in
    asset.*) ;;
    *) continue ;;
  esac
  if printf '%s\n' "$KEEP_LIST" | grep -qxF "$name"; then
    continue
  fi
  size_kb="$(du -sk "$candidate" | awk '{print $1}')"
  rm -rf "$candidate"
  removed=$((removed + 1))
  reclaimed_kb=$((reclaimed_kb + size_kb))
done

if [ "$removed" -eq 0 ]; then
  echo "    cdk.out prune: no orphaned assets (tree is clean)."
  exit 0
fi

if [ "$reclaimed_kb" -ge 1048576 ]; then
  reclaimed="$((reclaimed_kb / 1048576)) GB"
elif [ "$reclaimed_kb" -ge 1024 ]; then
  reclaimed="$((reclaimed_kb / 1024)) MB"
else
  reclaimed="${reclaimed_kb} KB"
fi
echo "    cdk.out prune: removed ${removed} orphaned asset dir(s), reclaimed ${reclaimed}."
exit 0
