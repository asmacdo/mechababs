#!/usr/bin/env bash
#
# run_in_podman.sh — run the mechababs e2e against the test-docker cluster config,
# inside the slurm-docker-ci container, under ROOTLESS podman.
#
# It installs the bind-mounted checkout into the container's python and runs the
# packaged scenario against it:
#   1. `pip install -e /mechababs`  (the code under test, on PATH as `mechababs`)
#   2. `pytest <the packaged suite> --cluster-config <test-docker.yaml>
#                                   --mechababs /mechababs@<its ref>`
# The scenario itself then runs `mechababs campaign init` in a fixture study, pinning
# that same `/mechababs@<ref>` — so the campaign the scenario builds records the code
# under test, and the campaign-building code a user hits is the code this exercises
# (docs/overview.md: dev exercises prod's exact paths, so dev validates prod).
#
# `git clone` takes a local path, so the pin is a mount path here and a public URL in
# prod; that is the only difference. Everything else dev-specific is a VALUE handed to
# the scenario — which checkout, which cluster config, and the container wrapped around
# them — never a separate route into it.
#
# slurm-docker-ci is CentOS 7 (glibc 2.17), so the campaign environment `campaign init`
# resolves here needs the same version caps a real old-glibc cluster needs — otherwise
# uv falls back to source builds the image's gcc 4.8 cannot compile. Those caps are NOT
# a harness special-case: they are `env_constraints` in
# examples/clusters/test-docker.yaml, the same cluster-axis field
# examples/clusters/sherlock.yaml uses, so this rung exercises that path rather than
# routing around it.
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
#                                     set on the simbids app config) creates a nested
#                                     user+PID namespace and mounts a fresh /proc onto
#                                     it. The kernel only allows that when the caller
#                                     has a FULLY-VISIBLE /proc, but podman MASKS
#                                     /proc paths by default -> "mount proc: operation
#                                     not permitted". systempaths=unconfined unmasks
#                                     /proc so the nested mount is allowed. It relaxes
#                                     THIS container's view of /proc, not host
#                                     privilege — container-root still maps to the
#                                     unprivileged host user. (Stages that run no inner
#                                     container don't need it; it stays for the ones
#                                     that will.)
#
# The checkout is mounted READ-WRITE, not :ro. `pip install -e` has setuptools_scm
# write `mechababs/_version.py` and an egg-info dir into the source tree; both are
# gitignored, so this cannot dirty the worktree, and rootless podman maps the writes
# to the invoking user rather than real root.
#
# The fixture study and its caches live on a host bind mount at
# $MECHABABS_E2E_WORKDIR, mounted at the SAME absolute path inside the container.
# Same-path is deliberate: babs bakes *absolute* RIA-store paths at init, so building
# at an identical host==container path is what lets a study — and the derivatives in
# it — stay operable on the host after the run. They persist regardless of --rm (they
# live on the host, not the container layer); MECHABABS_E2E_KEEP=1 only additionally
# keeps the *container* for post-mortem. The uv cache lives there too, so a second run
# resolves the campaign environment from disk instead of the network.
#
# Host-prep ONCE first — seed the container dataset the app configs name:
#   datalad clone https://github.com/ReproNim/containers.git \
#       $MECHABABS_E2E_WORKDIR/containers
#   datalad -C $MECHABABS_E2E_WORKDIR/containers get images/bids/bids-simbids--0.0.3.sif
# A plain ReproNim/containers clone, no shim: upstream carries the simbids image, and
# babs main resolves it from the datalad-containers registration (PennLINC/babs#399).
# It sits under $MECHABABS_E2E_WORKDIR (default /tmp/mechababs-e2e), visible through
# the same-path workdir mount, so the app configs' `../containers` resolves beside
# each fixture study. Local rather than the GitHub URL because babs installs
# `container.source` into every derivative it inits. The fake BIDS input is NOT
# host-prep — the rawdata fixture generates it into the workdir cache, which persists
# across runs through the same mount.
#
# Usage (extra args pass straight through to pytest):
#   mechababs/testing/e2e/run_in_podman.sh
#   mechababs/testing/e2e/run_in_podman.sh -k test_spine
#   MECHABABS_E2E_KEEP=1 mechababs/testing/e2e/run_in_podman.sh   # keep the container
set -euo pipefail

# mechababs/testing/e2e/ -> the worktree root (the suite ships inside the package).
# Unlike the scenario itself, this script only makes sense from a checkout: the dev
# campaign's mechababs pin IS that checkout. It is excluded from the distribution.
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
echo "REPO=$REPO" >&2

# The ref the scenario's campaigns pin: this checkout at the branch or tag it is on.
# `campaign init --mechababs URL@REF` resolves REF with `git clone --branch`, which takes
# a branch or a tag and not a bare sha — so check that here, before a long run
# discovers it.
REF="$(git -C "$REPO" symbolic-ref --short --quiet HEAD \
    || git -C "$REPO" describe --tags --exact-match 2>/dev/null || true)"
if [ -z "$REF" ]; then
    echo "error: $REPO is on a detached HEAD with no exact tag, so there is no branch or" >&2
    echo "    tag for the campaign pin to clone. Check out a branch or tag first." >&2
    exit 2
fi

# The campaign pin CLONES $REF, so uncommitted work would be absent from the campaign
# under test. Refuse rather than quietly validate your last commit.
DIRTY="$(git -C "$REPO" status --porcelain)"
if [ -n "$DIRTY" ]; then
    echo "error: $REPO is dirty. The scenario's campaign clones $REF, so this run would test" >&2
    echo "    your last commit and silently ignore the working tree:" >&2
    echo "$DIRTY" >&2
    exit 2
fi

# A worktree's .git is a FILE pointing at the main repo's common git dir; cloning
# /mechababs (what the campaign pin does) needs that dir reachable at the same path
# inside the container. Mount it (a no-op extra mount for a normal checkout).
GIT_COMMON_DIR="$(cd "$REPO" && git rev-parse --git-common-dir)"
REAL_GIT_DIR="$(cd "$GIT_COMMON_DIR" && pwd)"
EXTRA_MOUNT=()
[ "$REAL_GIT_DIR" != "$REPO/.git" ] && EXTRA_MOUNT=(-v "$REAL_GIT_DIR:$REAL_GIT_DIR")

# Bind-mount the workdir at the SAME absolute path inside the container, and build the
# fixture studies there (via MECHABABS_E2E_WORKDIR, passed in below) instead of the
# container's ephemeral /scratch layer. host==container path is what makes babs's
# init-time *absolute* RIA-store paths resolve on the host afterwards, so a study
# survives as a real, operable dataset — no `podman cp`, no dead /scratch abspaths. (One
# exception: a campaign's mechababs pin is `/mechababs`, the container-local mount of
# the checkout, so that source is not resolvable on the host.)
# (Same idiom as $REAL_GIT_DIR above.) The container dataset lives under the workdir
# too, so the app config's `../containers` resolves through this one mount — no
# separate container mount needed.
MECHABABS_E2E_WORKDIR="${MECHABABS_E2E_WORKDIR:-/tmp/mechababs-e2e}"
mkdir -p "$MECHABABS_E2E_WORKDIR"
WORKDIR_MOUNT=(-v "$MECHABABS_E2E_WORKDIR:$MECHABABS_E2E_WORKDIR")
if [ ! -e "$MECHABABS_E2E_WORKDIR/containers/images/bids/bids-simbids--0.0.3.sif" ]; then
    echo "note: no container dataset at $MECHABABS_E2E_WORKDIR/containers — seed it first:" >&2
    echo "    datalad clone https://github.com/ReproNim/containers.git $MECHABABS_E2E_WORKDIR/containers" >&2
    echo "    datalad -C $MECHABABS_E2E_WORKDIR/containers get images/bids/bids-simbids--0.0.3.sif" >&2
fi

# One id per run, used for the container name so it is obvious which container produced
# which studies. $RANDOM as well as $$ because PIDs are recycled, and under
# MECHABABS_E2E_KEEP a container from an earlier run is still around to collide with.
RUN_ID="$$-$RANDOM"
echo "fixture studies land under $MECHABABS_E2E_WORKDIR/e2e-study-* (remove stale ones" >&2
echo "    with rm -rf $MECHABABS_E2E_WORKDIR/e2e-study-*)" >&2

# The uv cache on the host bind mount, so the campaign environment the scenario resolves
# is downloaded once and reused by every later run. Without it each run re-fetches
# mechababs' + babs' whole dependency closure into a container layer that --rm discards.
UV_CACHE="$MECHABABS_E2E_WORKDIR/.uv-cache"
mkdir -p "$UV_CACHE"

# Forward BABS_SPEC (the babs ref under test) into the container if set. Unset, the
# suite pins babs main itself (see the `babs_pin` fixture: the app configs name a
# native ReproNim/containers layout, which only PennLINC/babs#399 resolves, and no
# release carries it) — so this is the OVERRIDE, not the only way to get a sane pin.
# An https URL must be public (the container clones anonymously); a local path works
# too, as long as it is under $MECHABABS_E2E_WORKDIR, which is bind-mounted at the
# same path inside the container. That is how an unpushed branch gets tested.
BABS_SPEC_ENV=()
[ -n "${BABS_SPEC:-}" ] && BABS_SPEC_ENV=(-e "BABS_SPEC=$BABS_SPEC")

# The container is always NAMED, so the Ctrl-C handler below has something to address.
# The studies persist on the host bind mount regardless of --rm.
# MECHABABS_E2E_KEEP=1 additionally keeps the *container* (drops --rm) for post-mortem of
# the container itself.
CONTAINER="mechababs-e2e-$RUN_ID"
RM_FLAG=(--rm)
if [ -n "${MECHABABS_E2E_KEEP:-}" ]; then
    RM_FLAG=()
    echo "KEEP: container $CONTAINER persists (the studies are already on the host" >&2
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
# run the container unattended (verified). Removing the CONTAINER covers every later
# interrupt, and is what actually stops the work, since pytest, babs and the inner
# singularity job all live inside it.
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

# Install the checkout into the container's python, then run the packaged scenario
# against it. Extra args ("$@") pass through to pytest, word boundaries preserved (so
# e.g. `-k "a or b"` survives as one arg).
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
    -v "$REPO":/mechababs \
    ${EXTRA_MOUNT[@]+"${EXTRA_MOUNT[@]}"} \
    ${WORKDIR_MOUNT[@]+"${WORKDIR_MOUNT[@]}"} \
    ${BABS_SPEC_ENV[@]+"${BABS_SPEC_ENV[@]}"} \
    -e "MECHABABS_E2E_WORKDIR=$MECHABABS_E2E_WORKDIR" \
    -e "MECHABABS_REF=$REF" \
    -e "UV_CACHE_DIR=$UV_CACHE" \
    docker.io/pennlinc/slurm-docker-ci:0.14 \
    bash -c '
        set -e
        # Container-only prep: the repo is host-owned but git runs as container-root,
        # and the image lacks uv (`campaign init` resolves the campaign env with it).
        git config --global --add safe.directory "*"
        command -v uv >/dev/null 2>&1 || pip install --quiet uv
        # The code under test, editable so `mechababs` on PATH IS the mount. pytest
        # comes with it via the `test` extra.
        pip install --quiet -e "/mechababs[test]"
        # cwd outside the checkout: pytest would otherwise pick up the repo pyproject
        # `testpaths = ["tests"]` and collect the unit suite instead of the scenario.
        cd "$MECHABABS_E2E_WORKDIR"
        # `-p no:cacheprovider` + PYTHONPYCACHEPREFIX keep pytest from writing a cache
        # and .pyc files into the bind-mounted checkout.
        PYTHONPYCACHEPREFIX=/tmp/pycache \
        python -m pytest -s -p no:cacheprovider \
            "$(python -c "import mechababs.testing as t; print(t.suite_path())")" \
            --cluster-config /mechababs/examples/clusters/test-docker.yaml \
            --mechababs "/mechababs@$MECHABABS_REF" \
            ${BABS_SPEC:+--babs "$BABS_SPEC"} \
            "$@"
    ' _ "$@" &
PODMAN_PID=$!

# `wait` rather than running podman in the foreground: this is the line that makes the
# trap above prompt. Keep podman's exit code as ours, so a failed e2e still fails here.
RC=0
wait "$PODMAN_PID" || RC=$?
exit "$RC"
