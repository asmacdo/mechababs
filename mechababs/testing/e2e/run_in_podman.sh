#!/usr/bin/env bash
#
# run_in_podman.sh — validate the test-docker cluster config from a dev campaign built
# out of this checkout, inside the slurm-docker-ci container, under ROOTLESS podman.
#
# It runs the two steps a USER runs, and nothing else:
#   1. `bootstrap.sh <dev campaign> --mechababs <this checkout>@<its ref>`
#   2. `mechababs test-cluster --cluster examples/clusters/test-docker.yaml` from it
# Everything dev-specific is a VALUE handed to those two steps — which checkout, which
# cluster config, and the container wrapped around them — never a separate route into
# the scenario. So the provisioning code a user's `test-cluster` runs is the code every
# dev run exercises (docs/overview.md: dev exercises prod's exact paths, so dev
# validates prod). Mirrors babs's tests/e2e_in_docker.sh.
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
# Both campaigns — the dev campaign this builds, and the throwaway one the scenario
# provisions beside it — live on a host bind mount at $MECHABABS_E2E_WORKDIR, mounted at
# the SAME absolute path inside the container. Same-path is deliberate: babs bakes
# *absolute* RIA-store paths at init, so building at an identical host==container path is
# what lets a campaign resolve — and stay operable — on the host after the run. They
# persist regardless of --rm (they live on the host, not the container layer);
# MECHABABS_E2E_KEEP=1 only additionally keeps the *container* for post-mortem.
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
# Usage (extra args pass straight through to `mechababs test-cluster`, so pytest args
# go after a literal `--`, the same as running test-cluster by hand):
#   mechababs/testing/e2e/run_in_podman.sh
#   mechababs/testing/e2e/run_in_podman.sh -- -k test_full_run
#   BABS_SPEC=https://github.com/<owner>/babs.git@<branch> \
#       mechababs/testing/e2e/run_in_podman.sh      # pin the babs under test
#   BABS_SPEC=$MECHABABS_E2E_WORKDIR/babs-under-test@<branch> \
#       mechababs/testing/e2e/run_in_podman.sh      # ... or an unpushed local clone
#   MECHABABS_E2E_KEEP=1 mechababs/testing/e2e/run_in_podman.sh   # keep the container
set -euo pipefail

# mechababs/testing/e2e/ -> the worktree root (the suite ships inside the package).
# Unlike the scenario itself, this script only makes sense from a checkout: the dev
# campaign's mechababs pin IS that checkout. It is excluded from the distribution.
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
echo "REPO=$REPO" >&2

# The pin bootstrap will clone: this checkout at the ref it is on. `git clone --branch`
# takes a branch or a tag, which is the same constraint `validate.clone_ref` enforces on
# a campaign's pins — applied here to the checkout, before a long run discovers it.
REF="$(git -C "$REPO" symbolic-ref --short --quiet HEAD \
    || git -C "$REPO" describe --tags --exact-match 2>/dev/null || true)"
if [ -z "$REF" ]; then
    echo "error: $REPO is on a detached HEAD with no exact tag, so there is no branch or" >&2
    echo "    tag for bootstrap to clone. Check out a branch or tag first." >&2
    exit 2
fi

# bootstrap CLONES $REF, so uncommitted work would be absent from the campaign under
# test. Refuse rather than quietly validate your last commit.
DIRTY="$(git -C "$REPO" status --porcelain)"
if [ -n "$DIRTY" ]; then
    echo "error: $REPO is dirty. The dev campaign clones $REF, so this run would test" >&2
    echo "    your last commit and silently ignore the working tree:" >&2
    echo "$DIRTY" >&2
    exit 2
fi

# A worktree's .git is a FILE pointing at the main repo's common git dir; a clone
# from /mechababs (what bootstrap.sh does) needs that dir reachable at the same
# path inside the container. Mount it (a no-op extra mount for a normal checkout).
GIT_COMMON_DIR="$(cd "$REPO" && git rev-parse --git-common-dir)"
REAL_GIT_DIR="$(cd "$GIT_COMMON_DIR" && pwd)"
EXTRA_MOUNT=()
[ "$REAL_GIT_DIR" != "$REPO/.git" ] && EXTRA_MOUNT=(-v "$REAL_GIT_DIR:$REAL_GIT_DIR")

# Bind-mount the workdir at the SAME absolute path inside the container, and build the
# campaigns there (via MECHABABS_E2E_WORKDIR, passed in below) instead of the
# container's ephemeral /scratch layer. host==container path is what makes babs's
# init-time *absolute* RIA-store paths resolve on the host afterwards, so a campaign
# survives as a real, operable dataset — no `podman cp`, no dead /scratch abspaths. (One
# exception: the dev campaign's own `code/mechababs` has `origin = /mechababs`, the
# container-local mount of the checkout, so that one remote is not resolvable on the host.)
# (Same idiom as $REAL_GIT_DIR above.) The shim is a sibling under the workdir, so the
# pipeline's `../repronim-containers-shim` resolves through this one mount — no
# separate shim mount needed.
MECHABABS_E2E_WORKDIR="${MECHABABS_E2E_WORKDIR:-/tmp/mechababs-e2e}"
mkdir -p "$MECHABABS_E2E_WORKDIR"
WORKDIR_MOUNT=(-v "$MECHABABS_E2E_WORKDIR:$MECHABABS_E2E_WORKDIR")
if [ ! -d "$MECHABABS_E2E_WORKDIR/repronim-containers-shim/.datalad" ]; then
    echo "note: no shim at $MECHABABS_E2E_WORKDIR/repronim-containers-shim — build it first:" >&2
    echo "    REPRONIM=$MECHABABS_E2E_WORKDIR/repronim-containers-shim tmp-repronim-container-shim.sh bids-simbids" >&2
fi

# The dev campaign: a fresh path per run, because bootstrap refuses an existing one. The
# scenario's own throwaway campaign lands beside it (test-cluster builds it in the
# campaign's parent, so the shim stays its sibling).
# One id for the run, shared by the campaign path and the container name, so it is
# obvious which container built which campaign. $RANDOM as well as $$ because PIDs are
# recycled, and under MECHABABS_E2E_KEEP a container from an earlier run is still around
# to collide with.
RUN_ID="$$-$RANDOM"
DEV_CAMPAIGN="$MECHABABS_E2E_WORKDIR/dev-campaign-$RUN_ID"
echo "dev campaign: $DEV_CAMPAIGN (remove stale ones with" >&2
echo "    rm -rf $MECHABABS_E2E_WORKDIR/dev-campaign-* $MECHABABS_E2E_WORKDIR/test-campaign-*)" >&2

# Forward BABS_SPEC (the babs ref under test) into the container if set, so the dev
# campaign PINS that babs — and the scenario's campaign, provisioned from these pins,
# inherits it. An https URL must be public (the container clones anonymously); a local
# path works too, as long as it is under $MECHABABS_E2E_WORKDIR, which is bind-mounted
# at the same path inside the container. That is how an unpushed branch gets tested.
BABS_SPEC_ENV=()
[ -n "${BABS_SPEC:-}" ] && BABS_SPEC_ENV=(-e "BABS_SPEC=$BABS_SPEC")

# The container is always NAMED, so the Ctrl-C handler below has something to address.
# The campaigns persist on the host bind mount regardless of --rm.
# MECHABABS_E2E_KEEP=1 additionally keeps the *container* (drops --rm) for post-mortem of
# the container itself.
CONTAINER="mechababs-e2e-$RUN_ID"
RM_FLAG=(--rm)
if [ -n "${MECHABABS_E2E_KEEP:-}" ]; then
    RM_FLAG=()
    echo "KEEP: container $CONTAINER persists (the campaigns are already on the host" >&2
    echo "    under $MECHABABS_E2E_WORKDIR). Remove the container with:" >&2
    echo "    podman rm $CONTAINER" >&2
fi

# Ctrl-C has to abort the run, and nothing about that is automatic here (#105). Three
# separate things swallow the interrupt:
#   1. Under `podman machine` (every macOS host), the podman CLI is a REMOTE client and
#      the signal never reaches the container at all.
#   2. Natively, podman's --sig-proxy does deliver it, but to the container's PID 1 —
#      tini — which forwards only to its DIRECT child: the `bash -c` wrapper below.
#      Non-interactive bash neither relays a signal to its foreground child nor runs a
#      trap until that child returns, and here that child is the whole e2e. So the run
#      continues to completion, which is the reported "nothing happens".
#   3. This script would have the same problem: bash defers a trap while a FOREGROUND
#      command runs, so a handler could not fire until podman exited on its own.
# So don't depend on signal delivery reaching the workload. Run podman in the
# background, `wait` for it (bash *does* interrupt `wait` to run a trap), and tear the
# container down explicitly — killing it kills everything inside, on both podman flavors.
# A TTY (`-t`) would fix the interactive case by giving the container a line discipline
# to deliver SIGINT to its foreground process group, but it does nothing for a redirected
# or CI run, and it would dress the log in pytest's terminal escapes. This works for both.
# Both halves of the teardown are needed. Stopping the CLIENT covers an interrupt in the
# first second or two, before the container exists: removing it by name would hit nothing,
# and bash does not reap the backgrounded client on exit, so it would go on to create and
# run the container unattended (verified — it reaches the end of bootstrap). Removing the
# CONTAINER covers every later interrupt, and is what actually stops the work, since
# pytest, babs and the inner singularity job all live inside it.
PODMAN_PID=
abort() {
    echo >&2
    echo "interrupted — stopping container $CONTAINER" >&2
    # An `if` rather than `[ … ] && kill …`, whose non-zero status under `set -e` would
    # skip the removal below. Empty only if the interrupt beat the assignment below.
    if [ -n "$PODMAN_PID" ]; then
        kill "$PODMAN_PID" 2>/dev/null || true
    fi
    podman rm --force --time 0 "$CONTAINER" >/dev/null 2>&1 || true
    exit 130
}
trap abort INT TERM

# Bootstrap the dev campaign, then validate the docker cluster config from it. Extra
# args ("$@") pass through to test-cluster, word boundaries preserved (so e.g.
# `-- -k "a or b"` survives as one pytest arg).
#
# `${A[@]+"${A[@]}"}` rather than a bare `"${A[@]}"`: under `set -u`, bash before 4.4
# reads an empty array's expansion as an unbound variable and aborts — and each of these
# arrays is empty in the default case, so the plain form breaks the script outright on a
# CentOS 7 / RHEL 7 login node (bash 4.2) or macOS's system bash 3.2.
podman run ${RM_FLAG[@]+"${RM_FLAG[@]}"} --name "$CONTAINER" -i \
    --platform linux/amd64 \
    -h slurmctl \
    --security-opt label=disable \
    --security-opt systempaths=unconfined \
    --device /dev/fuse \
    -v "$REPO":/mechababs:ro \
    ${EXTRA_MOUNT[@]+"${EXTRA_MOUNT[@]}"} \
    ${WORKDIR_MOUNT[@]+"${WORKDIR_MOUNT[@]}"} \
    ${BABS_SPEC_ENV[@]+"${BABS_SPEC_ENV[@]}"} \
    -e "MECHABABS_E2E_WORKDIR=$MECHABABS_E2E_WORKDIR" \
    -e "DEV_CAMPAIGN=$DEV_CAMPAIGN" \
    -e "MECHABABS_REF=$REF" \
    -e MECHABABS_E2E_SYSTEM_SITE_PACKAGES=1 \
    docker.io/pennlinc/slurm-docker-ci:0.14 \
    bash -c '
        set -e
        # Container-only prep: the repo is host-owned but git runs as
        # container-root, and the image lacks uv (bootstrap.sh needs it).
        git config --global --add safe.directory "*"
        command -v uv >/dev/null 2>&1 || pip install --quiet uv
        # 1. The dev campaign — prod bootstrap contract, with this checkout as the
        #    mechababs pin. --system-site-packages because this image is CentOS 7 and
        #    its 2015 toolchain cannot build the newest wheels; the same env var tells
        #    the scenario to pass it when it bootstraps its own campaign.
        /mechababs/bootstrap.sh "$DEV_CAMPAIGN" \
            --mechababs "/mechababs@$MECHABABS_REF" \
            ${BABS_SPEC:+--babs "$BABS_SPEC"} \
            ${MECHABABS_E2E_SYSTEM_SITE_PACKAGES:+--system-site-packages}
        # 2. Validate from that campaign — the user-facing command, unmodified. The
        #    config is read from the checkout by path; configure copies it in.
        cd "$DEV_CAMPAIGN"
        . .venv/bin/activate
        mechababs test-cluster \
            --cluster /mechababs/examples/clusters/test-docker.yaml "$@"
    ' _ "$@" &
PODMAN_PID=$!

# `wait` rather than running podman in the foreground: this is the line that makes the
# trap above prompt. Keep podman's exit code as ours, so a failed e2e still fails here.
RC=0
wait "$PODMAN_PID" || RC=$?
exit "$RC"
