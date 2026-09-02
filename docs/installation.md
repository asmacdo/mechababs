# mechababs — installation & prerequisites

What must be in place on an HPC before you validate a cluster config with `test-cluster` or run a campaign.
Do this, then follow [cluster-config-and-testing-tutorial.md](cluster-config-and-testing-tutorial.md).
Site-specific steps use UMass Unity as the worked example.

## On PATH (login *and* compute nodes)

- `git` — **≥ 2.25** (babs jobs use `git sparse-checkout`)
- `uv` — builds the campaign venv. Missing? `curl -LsSf https://astral.sh/uv/install.sh | sh` (lands in `~/.local/bin`).
- `apptainer` or `singularity`
- `git-annex` — the one most often missing. Install once into scratch:
  `datalad-installer git-annex -m datalad/git-annex:release --install-dir "$WS/tools" -E "$WS/tools/annex-env.sh"`, then `source "$WS/tools/annex-env.sh"` in each shell.

Verify before continuing:

```bash
for t in git uv apptainer git-annex datalad; do command -v $t || echo "MISSING: $t"; done
```

Jobs need a modern `git` (≥ 2.25, for `sparse-checkout`) and git-annex on PATH too — a login-node git *module* doesn't reach the compute nodes, so the cluster config's `script_preamble` must put both on the job PATH (see `examples/clusters/unity.yaml`).

## Scratch, not home

The campaign venv and RIA stores are large — put them on fast scratch, never home/`/tmp`.

- Put the study, and so the campaign inside it, on scratch. Validating a cluster is a separate space:
  `test-cluster --scratch-path` says where its throwaway study, the container dataset
  beside it, and its caches go — put that on scratch too.
- No persistent scratch (Unity)? `ws_allocate mechababs 30`, then `WS=$(ws_find mechababs)` for the live path.
- Unity `$HOME` is quota'd and `/tmp` is `noexec` — redirect caches onto the workspace:

  ```bash
  export UV_CACHE_DIR=$WS/.uv-cache
  export APPTAINER_CACHEDIR=$WS/.apptainer-cache APPTAINER_TMPDIR=$WS/.apptainer-tmp
  export PROOT_TMP_DIR=$WS/.proot-tmp
  ```

## The container dataset

App configs name a [ReproNim/containers](https://github.com/ReproNim/containers) dataset as their `container.source`, and babs installs it into every derivative it inits.
The starters use the GitHub URL, which works anywhere but pays a fresh clone per cell.
For a real sweep, clone it once onto scratch and point the app configs at that path instead (absolute, or relative to the study root):

```bash
datalad clone https://github.com/ReproNim/containers.git $WS/containers
```

babs resolves the image out of that dataset's datalad-containers registration, which needs a babs carrying `PennLINC/babs#399`.
It is merged but in no release, so pin babs by git ref at `campaign init` (`--babs https://github.com/PennLINC/babs.git@main`).

## The campaign venv is the only venv you need

`campaign init` builds each campaign its own venv from the campaign's `uv.lock` (pinned babs + mechababs), and that venv is what operates the campaign.
`mechababs` refuses to run outside it, a guard against a stray ambient install, and refuses when the venv no longer matches the lock.
Select and activate in one step, in every new shell:

```bash
source .mechababs/campaigns/<label>/env.sh
```

Run under `tmux`/`screen`; a login-node disconnect kills a long run.

## Then

Follow [cluster-config-and-testing-tutorial.md](cluster-config-and-testing-tutorial.md) to
write your cluster config and validate it with `mechababs test-cluster`.
