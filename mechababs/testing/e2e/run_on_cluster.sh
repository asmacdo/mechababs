#!/usr/bin/env bash
#
# run_on_cluster.sh — validate a REAL cluster's config from a dev campaign built out of
# this checkout, on the cluster's login node. The counterpart to run_in_podman.sh (which
# does the same inside the slurm-docker-ci container); here the cluster IS the
# substrate, so there is no container — the campaign submits real SLURM jobs.
#
# It runs the two steps a USER runs, and nothing else:
#   1. `bootstrap.sh <dev campaign> --mechababs <this checkout>@<its ref>`
#   2. `mechababs test-cluster --cluster <your config>` from it
# The only dev-specific thing is the first step's VALUE — the mechababs pin is your
# checkout instead of a released ref — so a dev run exercises the same provisioning code
# a user's `test-cluster` runs (docs/overview.md: dev exercises prod's exact paths).
#
# That validates an HPC config more thoroughly than `babs check-setup`: it drives the
# whole campaign path (bootstrap -> configure -> add-dataset -> iterate: scaffold ->
# submit -> wait -> merge) and asserts a real derivative landed.
#
# Design: the invocation is UNIFORM across sites — every cluster runs the same command
# with its own config. Per-site differences (module loads, PATH, scratch roots) live in
# the cluster YAML's script_preamble, NOT in flags to this script.
#
# Prerequisites (see docs/cluster-config-and-testing-tutorial.md for the full walk):
#   - git, uv, and apptainer/singularity on PATH (bootstrap.sh needs git + uv; it
#     builds the campaign venv that runs the scenario, so no driver venv is needed)
#   - MECHABABS_E2E_WORKDIR set to cluster scratch (the campaigns + shim live there)
#   - the container shim built there ONCE:
#       REPRONIM=$MECHABABS_E2E_WORKDIR/repronim-containers-shim \
#           tmp-repronim-container-shim.sh bids-simbids
#   - BABS_SPEC (optional): pin a babs ref to test against an unmerged babs fix. The dev
#     campaign pins it, and the scenario's campaign inherits it from those pins.
#     Default is babs main.
#
# Usage (extra args pass through to `mechababs test-cluster`, so pytest args go after a
# literal `--`, the same as running test-cluster by hand):
#   ./mechababs/testing/e2e/run_on_cluster.sh ~/config/your-site.yaml
#   ./mechababs/testing/e2e/run_on_cluster.sh ~/config/your-site.yaml -- -k test_full_run
set -euo pipefail

usage() {
    echo "usage: $0 <cluster-config> [-- PYTEST_ARGS...]" >&2
}

# mechababs/testing/e2e/ -> the worktree root. Unlike the scenario itself, this script
# only makes sense from a checkout: the dev campaign's mechababs pin IS that checkout.
# It is excluded from the distribution.
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"

# 1. The config to validate. Resolved to an absolute path HERE, because this script
#    later cds into the dev campaign to run test-cluster — a relative path would be
#    resolved against the wrong directory, and only after the bootstrap had already
#    spent minutes. Checked for existence for the same reason: fail before the work.
CLUSTER="${1:-}"
if [ -z "$CLUSTER" ]; then
    echo "error: no cluster config given — that is what this validates." >&2
    usage
    exit 2
fi
shift
if [ ! -f "$CLUSTER" ]; then
    echo "error: no cluster config at $CLUSTER" >&2
    usage
    exit 2
fi
CLUSTER="$(cd "$(dirname "$CLUSTER")" && pwd)/$(basename "$CLUSTER")"

# 2. Scratch workdir must be set explicitly — the campaign venvs + RIA stores are
#    large and belong on a fast cluster filesystem, never a login-node home or /tmp.
if [ -z "${MECHABABS_E2E_WORKDIR:-}" ]; then
    echo "error: set MECHABABS_E2E_WORKDIR to cluster scratch (the campaigns + shim live there)." >&2
    exit 2
fi

# 3. The container shim is host-prep, built once (drops when PennLINC/babs#383 lands).
#    test-cluster checks this too; checking here fails before the campaign bootstrap,
#    which takes minutes.
if [ ! -d "$MECHABABS_E2E_WORKDIR/repronim-containers-shim/.datalad" ]; then
    echo "error: no shim at $MECHABABS_E2E_WORKDIR/repronim-containers-shim — build it once:" >&2
    echo "    REPRONIM=$MECHABABS_E2E_WORKDIR/repronim-containers-shim tmp-repronim-container-shim.sh bids-simbids" >&2
    exit 2
fi

# 4. Prod parity: a real cluster builds bootstrap's isolated venv, so this should be
#    unset (the container rung sets it only because CentOS7 can't build new wheels).
if [ -n "${MECHABABS_E2E_SYSTEM_SITE_PACKAGES:-}" ]; then
    echo "warning: MECHABABS_E2E_SYSTEM_SITE_PACKAGES is set — a real cluster should leave it" >&2
    echo "    unset so bootstrap builds prod's isolated venv. Unset it unless you know why." >&2
fi

# 5. A login-node disconnect kills the run; iterate itself warns, but flag it early.
if [ -z "${TMUX:-}" ] && [ -z "${STY:-}" ]; then
    echo "warning: not in tmux/screen — a disconnect will kill the run. Ctrl-C to bail." >&2
fi

# 6. The pin bootstrap will clone: this checkout at the ref it is on. `git clone
#    --branch` takes a branch or a tag, the same constraint `validate.clone_ref`
#    enforces on a campaign's pins — applied here to the checkout.
REF="$(git -C "$REPO" symbolic-ref --short --quiet HEAD \
    || git -C "$REPO" describe --tags --exact-match 2>/dev/null || true)"
if [ -z "$REF" ]; then
    echo "error: $REPO is on a detached HEAD with no exact tag, so there is no branch or" >&2
    echo "    tag for bootstrap to clone. Check out a branch or tag first." >&2
    exit 2
fi

# 7. bootstrap CLONES $REF, so uncommitted work would be absent from the campaign under
#    test. Refuse rather than quietly validate your last commit.
DIRTY="$(git -C "$REPO" status --porcelain)"
if [ -n "$DIRTY" ]; then
    echo "error: $REPO is dirty. The dev campaign clones $REF, so this run would test" >&2
    echo "    your last commit and silently ignore the working tree:" >&2
    echo "$DIRTY" >&2
    exit 2
fi

# The dev campaign: a fresh path per run, because bootstrap refuses an existing one. The
# scenario's own throwaway campaign lands beside it (test-cluster builds it in the
# campaign's parent, so the shim stays its sibling).
DEV_CAMPAIGN="$MECHABABS_E2E_WORKDIR/dev-campaign-$$-$RANDOM"
echo "dev campaign: $DEV_CAMPAIGN (remove stale ones with" >&2
echo "    rm -rf $MECHABABS_E2E_WORKDIR/dev-campaign-* $MECHABABS_E2E_WORKDIR/test-campaign-*)" >&2

"$REPO/bootstrap.sh" "$DEV_CAMPAIGN" \
    --mechababs "$REPO@$REF" \
    ${BABS_SPEC:+--babs "$BABS_SPEC"} \
    ${MECHABABS_E2E_SYSTEM_SITE_PACKAGES:+--system-site-packages}

# Validate from that campaign — the user-facing command, unmodified. `exec` so the
# exit code is test-cluster's.
cd "$DEV_CAMPAIGN"
# shellcheck source=/dev/null
. .venv/bin/activate
exec mechababs test-cluster --cluster "$CLUSTER" "$@"
