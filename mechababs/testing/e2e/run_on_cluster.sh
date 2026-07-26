#!/usr/bin/env bash
#
# run_on_cluster.sh — run the mechababs e2e scenario on a REAL cluster's login
# node, against a real cluster config. The counterpart to run_in_podman.sh (which
# runs everything inside the slurm-docker-ci container); here the cluster IS the
# substrate, so there is no container — pytest runs directly on the login node and
# the campaign submits real SLURM jobs.
#
# This validates an HPC config more thoroughly than `babs check-setup`: it drives
# the whole campaign path (bootstrap -> configure -> add-dataset -> iterate:
# scaffold -> submit -> wait -> merge) and asserts a real derivative landed.
#
# Design: the invocation is UNIFORM across sites — every cluster runs the same
# command. Per-site differences (module loads, PATH, scratch roots) live in the
# cluster YAML's script_preamble, NOT in flags to this script. So this wrapper only
# guards the environment contract and hands off to pytest; extra args pass through.
#
# Prerequisites (see docs/cluster-config-and-testing-tutorial.md for the full walk):
#   - a Python env with the test deps:  pip install -e '.[test]'
#   - git, uv, and apptainer/singularity on PATH
#   - MECHABABS_E2E_WORKDIR set to cluster scratch (campaign + shim live there)
#   - the container shim built there ONCE:
#       REPRONIM=$MECHABABS_E2E_WORKDIR/repronim-containers-shim \
#           tmp-repronim-container-shim.sh bids-simbids
#   - BABS_SPEC (optional): pin a babs ref to test against an unmerged babs fix.
#     Default is babs main.
#
# Usage (extra args pass straight through to pytest):
#   ./mechababs/testing/e2e/run_on_cluster.sh --cluster-config your-site.yaml
#   ./mechababs/testing/e2e/run_on_cluster.sh --cluster-config your-site.yaml -k test_full_run
set -euo pipefail

# The suite is this script's own directory: it ships inside the package, so there is
# no walk up to a repo root and this works from an installed copy too.
SUITE="$(cd "$(dirname "$0")" && pwd)"

# 1. Scratch workdir must be set explicitly — the campaign venv + RIA stores are
#    large and belong on a fast cluster filesystem, never a login-node home or /tmp.
if [ -z "${MECHABABS_E2E_WORKDIR:-}" ]; then
    echo "error: set MECHABABS_E2E_WORKDIR to cluster scratch (the campaign + shim live there)." >&2
    exit 2
fi

# 2. The container shim is host-prep, built once (drops when PennLINC/babs#383 lands).
if [ ! -d "$MECHABABS_E2E_WORKDIR/repronim-containers-shim/.datalad" ]; then
    echo "error: no shim at $MECHABABS_E2E_WORKDIR/repronim-containers-shim — build it once:" >&2
    echo "    REPRONIM=$MECHABABS_E2E_WORKDIR/repronim-containers-shim tmp-repronim-container-shim.sh bids-simbids" >&2
    exit 2
fi

# 3. Prod parity: a real cluster builds bootstrap's isolated venv, so this should be
#    unset (the container rung sets it only because CentOS7 can't build new wheels).
if [ -n "${MECHABABS_E2E_SYSTEM_SITE_PACKAGES:-}" ]; then
    echo "warning: MECHABABS_E2E_SYSTEM_SITE_PACKAGES is set — a real cluster should leave it" >&2
    echo "    unset so bootstrap builds prod's isolated venv. Unset it unless you know why." >&2
fi

# 4. A login-node disconnect kills the run; iterate itself warns, but flag it early.
if [ -z "${TMUX:-}" ] && [ -z "${STY:-}" ]; then
    echo "warning: not in tmux/screen — a disconnect will kill the run. Ctrl-C to bail." >&2
fi

# BABS_SPEC is read straight from the environment by the campaign fixture; the
# cluster config is passed through in "$@" as --cluster-config <name>.
exec pytest -s "$SUITE" "$@"
