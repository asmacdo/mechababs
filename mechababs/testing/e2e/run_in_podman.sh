#!/usr/bin/env bash
#
# run_in_podman.sh — run the mechababs e2e scenario inside the slurm-docker-ci
# container, under ROOTLESS podman.
#
# Increment 1 runs everything INSIDE pennlinc/slurm-docker-ci (bootstrap-across-
# the-boundary is a later fidelity step). This launches the container with the
# mechababs repo mounted and runs pytest inside. Mirrors babs's tests/e2e_in_docker.sh.
#
# Rootless: no root daemon, and container-root maps to the invoking host user via
# userns — so nothing here runs as real root and any host-touching bytes are
# user-owned (root-in / user-out). slurm-docker-ci comes up rootless with no
# --privileged (verified: podman 5.8.2, cgroups v2). SELinux is handled with
# `label=disable` rather than per-mount `:Z`: one of the mounts is the shared
# git-common-dir, and `:Z` would relabel it on the host and disturb sibling
# worktrees — disabling the label for this container relabels nothing. Two extras
# the nested workload needs, NEITHER of which adds a Linux capability or breaks
# root-in/user-out (we add ZERO caps — no --cap-add, no --privileged):
#   --device /dev/fuse                singularity mounts the squashfs SIF via FUSE,
#                                     and rootless podman doesn't expose it by
#                                     default (a device, not a cap).
#   --security-opt systempaths=unconfined
#                                     a babs job runs simbids via `singularity run`
#                                     INSIDE this container; apptainer (with --userns,
#                                     set on the simbids pipeline) creates a nested
#                                     user+PID namespace and mounts a fresh /proc onto
#                                     it. The kernel only allows that when the caller
#                                     has a FULLY-VISIBLE /proc, but podman MASKS
#                                     /proc paths by default -> "mount proc: operation
#                                     not permitted". systempaths=unconfined unmasks
#                                     /proc so the nested mount is allowed. It relaxes
#                                     THIS container's view of /proc, not host
#                                     privilege — container-root still maps to the
#                                     unprivileged host user. (Scaffold-only runs —
#                                     `babs init`, no inner container — don't need it.)
#
# The campaign is built on a host bind mount at $MECHABABS_E2E_WORKDIR, mounted at
# the SAME absolute path inside the container. Same-path is deliberate: babs bakes
# *absolute* RIA-store paths at init, so building at an identical host==container
# path is what lets the campaign resolve — and stay operable — on the host after the
# run. It persists regardless of --rm (it lives on the host, not the container
# layer); MECHABABS_E2E_KEEP=1 only additionally keeps the *container* for post-mortem.
#
# Host-prep ONCE first — build the shim (the prod container-shim command; dies at
# babs#383):
#   REPRONIM=$MECHABABS_E2E_WORKDIR/repronim-containers-shim \
#       tmp-repronim-container-shim.sh bids-simbids
# It sits as a campaign sibling under $MECHABABS_E2E_WORKDIR (default /tmp/mechababs-e2e),
# visible through the same-path workdir mount, so configure resolves the pipeline's
# `../repronim-containers-shim`. The fake BIDS input is NOT host-prep — the rawdata
# fixture generates it into the workdir cache, which persists across runs through the
# same workdir mount (no separate cache mount needed).
#
# Usage (extra args pass straight through to pytest):
#   mechababs/testing/e2e/run_in_podman.sh
#   mechababs/testing/e2e/run_in_podman.sh --cluster-config test-docker.yaml
#   MECHABABS_E2E_KEEP=1 mechababs/testing/e2e/run_in_podman.sh   # keep the container
set -euo pipefail

# mechababs/testing/e2e/ -> the worktree root (the suite ships inside the package).
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"       # the mechababs worktree root
echo "REPO=$REPO" >&2

# A worktree's .git is a FILE pointing at the main repo's common git dir; a clone
# from /mechababs (what bootstrap.sh does) needs that dir reachable at the same
# path inside the container. Mount it (a no-op extra mount for a normal checkout).
GIT_COMMON_DIR="$(cd "$REPO" && git rev-parse --git-common-dir)"
REAL_GIT_DIR="$(cd "$GIT_COMMON_DIR" && pwd)"
EXTRA_MOUNT=()
[ "$REAL_GIT_DIR" != "$REPO/.git" ] && EXTRA_MOUNT=(-v "$REAL_GIT_DIR:$REAL_GIT_DIR")

# Bind-mount the workdir at the SAME absolute path inside the container, and build
# the campaign there (via MECHABABS_E2E_WORKDIR, passed in below) instead of the
# container's ephemeral /scratch layer. host==container path is what makes babs's
# init-time *absolute* RIA-store paths resolve on the host afterwards, so the
# campaign survives as a real, operable dataset — no `podman cp`, no dead /scratch
# abspaths. (Same idiom as $REAL_GIT_DIR above.) The shim is a sibling under the
# workdir, so the pipeline's `../repronim-containers-shim` resolves through this one
# mount — no separate shim mount needed.
MECHABABS_E2E_WORKDIR="${MECHABABS_E2E_WORKDIR:-/tmp/mechababs-e2e}"
mkdir -p "$MECHABABS_E2E_WORKDIR"
WORKDIR_MOUNT=(-v "$MECHABABS_E2E_WORKDIR:$MECHABABS_E2E_WORKDIR")
if [ ! -d "$MECHABABS_E2E_WORKDIR/repronim-containers-shim/.datalad" ]; then
    echo "note: no shim at $MECHABABS_E2E_WORKDIR/repronim-containers-shim — build it first:" >&2
    echo "    REPRONIM=$MECHABABS_E2E_WORKDIR/repronim-containers-shim tmp-repronim-container-shim.sh bids-simbids" >&2
fi

# Forward BABS_SPEC (the babs ref under test) into the container if set, so the
# campaign fixture's bootstrap pins that babs. Needed until `babs status --json`
# (PennLINC/babs#387) is in babs main — before then the full-run tier needs a branch
# that has it. A public https URL is required (the container clones anonymously).
BABS_SPEC_ENV=()
[ -n "${BABS_SPEC:-}" ] && BABS_SPEC_ENV=(-e "BABS_SPEC=$BABS_SPEC")

# The campaign persists on the host bind mount ($MECHABABS_E2E_WORKDIR/test-campaign-*)
# regardless of --rm. MECHABABS_E2E_KEEP=1 additionally keeps the *container* (drops
# --rm, names it) for post-mortem of the container itself.
RM_FLAG=(--rm)
NAME_FLAG=()
if [ -n "${MECHABABS_E2E_KEEP:-}" ]; then
    CONTAINER="mechababs-e2e-$$"
    RM_FLAG=()
    NAME_FLAG=(--name "$CONTAINER")
    echo "KEEP: container $CONTAINER persists (the campaign is already on the host at" >&2
    echo "    $MECHABABS_E2E_WORKDIR/test-campaign-*). Remove the container with:" >&2
    echo "    podman rm $CONTAINER" >&2
fi

# Run the e2e scenario: pytest inside the container. Extra args ("$@") pass through,
# word boundaries preserved (so e.g. -k "a or b" survives as one pytest arg).
podman run "${RM_FLAG[@]}" "${NAME_FLAG[@]}" -i \
    --platform linux/amd64 \
    -h slurmctl \
    --security-opt label=disable \
    --security-opt systempaths=unconfined \
    --device /dev/fuse \
    -v "$REPO":/mechababs:ro \
    "${EXTRA_MOUNT[@]}" \
    "${WORKDIR_MOUNT[@]}" \
    "${BABS_SPEC_ENV[@]}" \
    -e "MECHABABS_E2E_WORKDIR=$MECHABABS_E2E_WORKDIR" \
    -e MECHABABS_E2E_SYSTEM_SITE_PACKAGES=1 \
    docker.io/pennlinc/slurm-docker-ci:0.14 \
    bash -c '
        set -e
        # Container-only prep: the repo is host-owned but git runs as
        # container-root, and the image lacks uv (bootstrap.sh needs it).
        git config --global --add safe.directory "*"
        command -v uv >/dev/null 2>&1 || pip install --quiet uv
        # pytest reads the repo from a :ro mount, so redirect the bytecode cache off
        # it and skip the on-disk cache (cannot write to /mechababs).
        PYTHONPYCACHEPREFIX=/tmp/pyc pytest -p no:cacheprovider -x -q \
            /mechababs/mechababs/testing/e2e/ "$@"
    ' _ "$@"
