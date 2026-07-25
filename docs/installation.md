# mechababs — installation & prerequisites

What must be in place on an HPC before you validate a cluster profile with the e2e.
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

Jobs need a modern `git` (≥ 2.25, for `sparse-checkout`) and git-annex on PATH too — a login-node git *module* doesn't reach the compute nodes, so the cluster profile's `script_preamble` must put both on the job PATH (see `examples/clusters/unity.yaml`).

## Scratch, not home

The campaign venv and RIA stores are large — put them on fast scratch, never home/`/tmp`.

- Set `MECHABABS_E2E_WORKDIR` to scratch.
- No persistent scratch (Unity)? `ws_allocate mechababs 30`, then `WS=$(ws_find mechababs)` for the live path.
- Unity `$HOME` is quota'd and `/tmp` is `noexec` — redirect caches onto the workspace:

  ```bash
  export UV_CACHE_DIR=$WS/.uv-cache
  export APPTAINER_CACHEDIR=$WS/.apptainer-cache APPTAINER_TMPDIR=$WS/.apptainer-tmp
  export PROOT_TMP_DIR=$WS/.proot-tmp
  ```

## Container shim (one-time, temporary)

**This is a temporary workaround and won't be needed soon.**
Vanilla babs reads container images from its own hardcoded path, so the shim re-registers them there — and today it must be a clone of [ReproNim/containers](https://github.com/ReproNim/containers) specifically.
Once [PennLINC/babs#383](https://github.com/PennLINC/babs/issues/383) lands, babs will resolve the image from **any** datalad-containers dataset directly (not just ReproNim/containers), and this step — the ReproNim coupling and the script — go away entirely.

Until then, build it as a **sibling of your campaigns**:

```bash
REPRONIM=$WS/repronim-containers-shim ./tmp-repronim-container-shim.sh bids-simbids
```

`bids-simbids` is built from Docker Hub (needs the apptainer/proot redirects above); real apps (`bids-mriqc`, `bids-fmriprep`) are fetched.
Idempotent — to rebuild cleanly, point `REPRONIM` at a fresh path.

## Driver venv (to run the e2e)

`run_on_cluster.sh` runs pytest to drive the campaign. It needs its own env, and it **must be a venv**:

```bash
uv venv && source .venv/bin/activate
uv pip install -e '.[test]'
```

Run under `tmux`/`screen` — a login-node disconnect kills the run.

**Two venvs, don't cross them.** This driver venv only runs pytest — it has no `babs` or `con-duct`.
Each campaign gets its **own** venv that `bootstrap.sh` builds (pinned babs + mechababs + con-duct), and that is what actually operates the campaign — `mechababs` refuses to run outside its campaign venv (a guard against a stray ambient install).
So to poke a campaign after the run (`mechababs status`, another `iterate`), activate *its* venv, not this one:

```bash
source <campaign>/.venv/bin/activate
```

## Then

Follow [cluster-config-and-testing-tutorial.md](cluster-config-and-testing-tutorial.md) to write `examples/clusters/<site>.yaml` and validate it.
