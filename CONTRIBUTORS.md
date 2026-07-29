# Contributing to mechababs

## e2e testing

The e2e harness drives the **real** campaign CLI end to end:
`bootstrap.sh` → `configure` → `add-dataset` → `iterate` (scaffold → submit → merge).
It asserts on the resulting ledger, babs project, and produced derivative.
The BIDS-app used is [simbids](https://github.com/PennLINC/simbids) as a fast stand-in BIDS app so a full submit→merge runs in minutes instead of hours.

## Two modes: local container vs real cluster

The same test body runs two ways:

- **against a local container running slurm** — for development, and the natural
  candidate for CI when there is one. That's the rest of this doc.
- **against a real cluster** — a user-facing feature to validate an HPC's config
  (and exercise our portability), exposed as `mechababs test-cluster`. That path is a
  tutorial in its own right:
  [docs/cluster-config-and-testing-tutorial.md](docs/cluster-config-and-testing-tutorial.md).

Because it is user-facing, the suite ships inside the package
(`mechababs/testing/e2e/`) rather than in `tests/`: `test-cluster` has to find it
wherever mechababs is installed, not only in a checkout. `tests/` is the unit suite,
which never leaves the repo.

## Running the tests (local container)

The suite lives in `mechababs/testing/e2e/` (inside the package, so it ships with an install). Set `MECHABABS_E2E_WORKDIR` to a scratch dir where
the campaign and the container shim live as siblings.

TEMPORARY PREREQUISITE: BUILD REPRONIM_CONTAINERS_SHIM
You should only need to build this shim once.
This shim is a fork of ReproNim/containers that includes simbids, and modifies the paths to workaround a babs RFE.

```bash
REPRONIM=$MECHABABS_E2E_WORKDIR/repronim-containers-shim tmp-repronim-container-shim.sh bids-simbids
```

NOTE: the mechababs `campaigns` and the container shim live **as siblings** (a pipeline resolves its container as
`../repronim-containers-shim`, so the two must share a parent).

### Local container (much faster!)

Runs the e2e scenario under rootless podman on a container with working SLURM.

Host prerequisites: `podman`, `apptainer` (for the one-time shim build), and
`/dev/fuse` (podman is invoked with `--device /dev/fuse` so in-container singularity
can mount the SIF).

1. Pick a scratch dir outside the repo:

   ```bash
   export MECHABABS_E2E_WORKDIR=~/mechababs-e2e-scratch
   ```

2. Build the container shim **once** (clones ReproNim/containers and builds the
   simbids SIF from Docker Hub — several minutes; reused thereafter):

   ```bash
   REPRONIM=$MECHABABS_E2E_WORKDIR/repronim-containers-shim \
       tmp-repronim-container-shim.sh bids-simbids
   ```

   This is the temporary manual container shim (drops when `PennLINC/babs#383` lands);
   see the reference doc's "Manual shims" section.

3. Run the suite (extra args pass through to `mechababs test-cluster`, so pytest args
   go after a literal `--`, exactly as when you run `test-cluster` by hand):

   ```bash
   mechababs/testing/e2e/run_in_podman.sh
   # or a single test:
   mechababs/testing/e2e/run_in_podman.sh -- -k test_full_run
   ```

**What it does — the same two steps a user runs.** Inside the container it bootstraps a
*dev campaign* whose mechababs pin is your checkout, then runs
`mechababs test-cluster --cluster examples/clusters/test-docker.yaml` from it. There is no
dev-only way into the scenario, so the provisioning code a user's `test-cluster` runs is
the code your run exercises. Because bootstrap *clones* your branch, the script refuses a
dirty tree — commit first, or you would be testing your last commit — and needs a branch
or tag checked out (a bare detached commit is not something `git clone --branch` can pin).

The fake BIDS input generates itself on the first run into `$MECHABABS_E2E_WORKDIR/e2e-cache/`
(reused after, and visible on the host through the workdir mount).
Both campaigns — the dev campaign and the throwaway one `test-cluster` provisions beside
it — are built on the host workdir mount, at the same absolute path they have inside the
container, so they outlive the run as real, operable datasets (babs bakes absolute RIA
paths at init, so an identical host==container path is what keeps them resolvable). They
accumulate; clean up with:

```bash
rm -rf $MECHABABS_E2E_WORKDIR/dev-campaign-* $MECHABABS_E2E_WORKDIR/test-campaign-*
```

`MECHABABS_E2E_KEEP=1` additionally keeps the *container*, for post-mortem of the
container itself.

**Ctrl-C aborts the run.** It stops the container and exits 130. The campaigns built so
far stay on the host workdir, half-finished; clean them up the same way.

**babs under test.** The suite runs on babs `main` by default. To test against an
unmerged babs fix, pin a babs ref — the dev campaign pins it, and the scenario's campaign
inherits it from those pins:

```bash
export BABS_SPEC=https://github.com/<owner>/babs.git@<branch>
```
