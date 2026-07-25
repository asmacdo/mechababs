# mechababs — cluster config & testing tutorial

Bringing mechababs to a new HPC is two steps: write one small **cluster profile**,
then **validate it by running the real e2e suite on your cluster**. That second
step is a stronger check than `babs check-setup` — it drives the whole campaign
path (bootstrap → configure → add-dataset → iterate: scaffold → submit → wait →
merge) and asserts a real derivative landed, so it catches HPC-specific breakage a
scaffold-only deploy would miss.

## What a cluster profile is

A cluster profile is small. It answers two questions: **how to enter the
campaign environment**, and **where per-job scratch lives**. Here is the bundled
`examples/clusters/dartmouth.yaml` in full:

```yaml
script_preamble: |
  # campaign venv (abspath substituted at compose time by merge_config)
  source "{{MECHABABS_VENV}}/bin/activate"
  export JOB_TMP="/scratch/${USER}/sjob-tmp/${SLURM_ARRAY_JOB_ID}_${SLURM_ARRAY_TASK_ID}"
  mkdir -p "${JOB_TMP}"
  trap 'rm -rf "${JOB_TMP}"' EXIT

job_compute_space: "/scratch/${USER}"
```

- **`script_preamble`** — shell that runs at the top of every job: activate the
  campaign venv (via the `{{MECHABABS_VENV}}` placeholder, which `merge_config.py`
  substitutes with the campaign's venv abspath at compose time — leave it literally
  as written), set a per-job `JOB_TMP` under your scratch, and clean it up on exit.
- **`job_compute_space`** — the scratch base the job works in.

**What is *not* here (a common misconception):** SLURM resources
(`cluster_resources`) and the container's `-B $JOB_TMP:/tmp` bind live on the
**pipeline** axis, in `pipelines/*.yaml`, not the cluster file. The cluster profile
only *supplies* `$JOB_TMP` (via the preamble) and `job_compute_space`; the pipeline
YAMLs consume `$JOB_TMP`. So "how big/long a job is" is pipeline config; "where
scratch is and how to enter the env" is cluster config.

`examples/clusters/unity.yaml` is the best real-world adaptation to read: Unity ships no
git-annex on the compute nodes, so its preamble prepends a workspace-local
git-annex build to `PATH`, and it roots scratch under an allocated HPC workspace
(`/scratch4/workspace/${USER}-mechababs`) because Unity has no persistent per-user
`/scratch`. Same two keys, site-specific values.

## Known gap: some site config still leaks into the pipeline YAMLs

One honest caveat before you start — the config-decoupling work we would most like
help with:

- **Site paths in pipeline YAMLs.** templateflow and the FreeSurfer license are
  bind-mounted from **hardcoded Dartmouth paths inside the fmriprep/mriqc pipeline
  YAMLs**. A new site must edit those binds in the pipeline files it uses, not just
  the cluster file. By rights a site path belongs on the cluster axis; today it
  doesn't. (The simbids test pipeline has no such binds, so the e2e below is
  unaffected — but a real fmriprep run will need this.)

Cluster and pipeline configs themselves are **campaign-owned**: they live in the
campaign's own `clusters/` and `pipelines/`, and `configure` copies the config you
name into the campaign and resolves it by name — so the config that produced a run
is committed alongside it. The files under `examples/` are starters to copy from,
not a directory the tool resolves against; using mechababs at your site needs no
fork of it.

## Add your cluster

The e2e validates a profile that lives in `examples/clusters/`, alongside the
bundled ones — so your site profile joins `dartmouth`/`unity`/`sherlock` as another
real-world starter. Working in a checkout:

1. Copy the closest starter: `cp examples/clusters/dartmouth.yaml examples/clusters/your-site.yaml`.
2. Edit `script_preamble`:
   - keep the `source "{{MECHABABS_VENV}}/bin/activate"` line exactly as-is,
   - set `JOB_TMP` to your scratch root,
   - add any `module load` / `PATH` lines your site needs (see `unity.yaml`).
3. Set `job_compute_space` to your scratch base.
4. If you'll run fmriprep/mriqc, point the templateflow / FS-license binds in those
   `examples/pipelines/*.yaml` at your site's paths (the gap above).
5. **Commit it.** The e2e bootstraps a throwaway campaign from the *committed*
   mechababs (a dirty tree is refused) and reads the profile from `examples/clusters/`,
   so `examples/clusters/your-site.yaml` must be committed before you validate. (A
   real campaign takes it by path instead — `configure --cluster <path>` copies it
   in — so production use needs no checkout.)

## Validate by running the e2e on your cluster

Run this on a **login node** — the cluster is the substrate, so there is no
container here.

Get the prerequisites in place first — see [installation.md](installation.md): the PATH tools, a scratch workspace and `MECHABABS_E2E_WORKDIR`, the container shim, and the driver venv.

> **ASPIRATIONAL (not yet built):** longer term this setup shrinks — because you
> bootstrap a campaign before running real data anyway, the intended end state is to
> validate from *inside* a campaign, e.g. `mechababs test-cluster --cluster
> your-site.yaml`. That subcommand does not exist yet — see [#98](https://github.com/con/mechababs/issues/98).

**Run it** (under `tmux`/`screen` — a login-node disconnect kills the run):

```bash
./tests/e2e/run_on_cluster.sh --cluster-config your-site.yaml
# or drive pytest directly:
pytest -s tests/e2e/ --cluster-config your-site.yaml
```

By default babs is pinned to `PennLINC/babs@main`; set `BABS_SPEC=<url@ref>` (a public https URL the campaign clones anonymously) only if your run needs an unmerged babs branch.

`run_on_cluster.sh` is a thin wrapper: it guards the environment contract above
(workdir set, shim built, site-packages unset, tmux) and hands off to pytest.
Every site runs the *same* command — per-site differences belong in your cluster
YAML, never in how you invoke this.

**What a green run means:** the suite bootstrapped a campaign, configured it with
*your* cluster profile, submitted real SLURM jobs, waited on them, merged, and
asserted a produced derivative landed in the output RIA. If it passes, your cluster
config produces derivatives — you're ready to point mechababs at real datasets.

## See also

- [reference.md](reference.md) — the config files and the rest of the CLI.
- [CONTRIBUTORS.md](../CONTRIBUTORS.md) — developing and testing mechababs itself,
  including the much-faster local-container test rung.
