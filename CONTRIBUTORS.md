# Contributing to mechababs

## Unit tests and lint

The unit suite lives in `tests/` and runs in a checkout's own environment:

```bash
uv sync --extra test
uv run python -m pytest tests/ -q
```

Lint is ruff, at the exact version `pyproject.toml` pins (`[tool.ruff] required-version`); CI installs the same one, so a green local run means a green CI run.

```bash
uvx ruff@0.16.4 check . && uvx ruff@0.16.4 format --check .
```

## The e2e scenario

The e2e drives the **real** CLI end to end, in a throwaway study: `campaign init`, `add-dataset`, then `iterate` through scaffold, submit and merge, asserting a real derivative landed.
The BIDS App is [SimBIDS](https://github.com/PennLINC/simbids), a fast stand-in, so a full submit-to-merge runs in minutes instead of hours.

The same scenario runs two ways:

- **against a real cluster**, as `mechababs test-cluster`, the user-facing way to validate a cluster config. That path is its own tutorial: [docs/cluster-config-and-testing-tutorial.md](docs/cluster-config-and-testing-tutorial.md).
- **against a local container running SLURM**, for development. The rest of this doc.

Because it is user-facing, the suite ships inside the package (`mechababs/testing/e2e/`) rather than in `tests/`: `test-cluster` has to find it wherever mechababs is installed, not only in a checkout.

There is no dev-only route into the scenario.
Dev and prod differ only in the values handed to it: which mechababs, which babs, which cluster config, and the container wrapped around them.

## Running the e2e locally (podman)

`mechababs/testing/e2e/run_in_podman.sh` runs the scenario under rootless podman, inside `pennlinc/slurm-docker-ci`, against `examples/clusters/test-docker.yaml`.
It installs your checkout into the container and hands the scenario `--mechababs /mechababs@<your ref>`, so the campaign the scenario builds pins the code under test.

Host prerequisites: `podman`, `datalad`, and `/dev/fuse` (in-container singularity mounts the SIF through FUSE).

1. Pick a scratch dir outside the repo. The fixture study, the container dataset, and the caches all land here, at the same absolute path inside the container.

   ```bash
   export MECHABABS_E2E_WORKDIR=~/mechababs-e2e-scratch
   ```

   Give the resolved path, not a symlink: the path is baked into what the fixtures build, and the container mounts only the resolved one.

2. Seed the container dataset once. The app configs resolve it as `../containers` beside each fixture study; local rather than the GitHub URL because babs installs it into every derivative it inits.

   ```bash
   datalad clone https://github.com/ReproNim/containers.git $MECHABABS_E2E_WORKDIR/containers
   datalad -C $MECHABABS_E2E_WORKDIR/containers get images/bids/bids-simbids--0.0.3.sif
   ```

3. Run the suite. Arguments pass through to pytest.

   ```bash
   mechababs/testing/e2e/run_in_podman.sh
   mechababs/testing/e2e/run_in_podman.sh -k test_spine      # one test
   ```

**The tree must be clean, on a branch or tag.** The scenario pins your checkout by ref and clones it, so uncommitted work is not what runs; the script refuses a dirty tree rather than pass while testing your last commit.

**babs under test.** The scenario's campaign gets the released babs by default, frozen by the campaign's lock.
To run against a branch, set `BABS_SPEC`; it takes anything `git clone` accepts, a local clone under the workdir included.

```bash
export BABS_SPEC=https://github.com/PennLINC/babs.git@main
```

Today that pin is required rather than optional: the released babs predates `PennLINC/babs#399`, without which the image cannot be resolved out of the ReproNim/containers clone.

**Reading the result.** The script's exit code is pytest's, so trust it; do not append `; echo` or anything else that would overwrite it.
The verdict is the `N passed` / `N failed` line.

**What persists.** The fixture studies and the campaigns inside them stay on the host under the workdir after the run, as real datalad datasets you can inspect with host `git` and `datalad`.
Their `.venv` was built inside the container and does not run from the host.
They accumulate; delete them from the workdir when done.
`MECHABABS_E2E_KEEP=1` additionally keeps the container for a post-mortem.
Ctrl-C stops the container and exits 130, leaving whatever was built half-finished.

## Docs

The docs describe the tool as it is now.
`docs/spec.md` is the design contract, `docs/use_cases.md` the user stories it answers to, and `docs/output_structure.md` the resulting layout; a behavior change updates them alongside the code.
New markdown is written one sentence per line, unwrapped.
