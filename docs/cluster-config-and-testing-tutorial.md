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

Write your profile wherever you keep site config and pass its path — `test-cluster`
takes a path, and `configure --cluster <path>` copies it into the campaign, so
nothing has to live in a checkout. Copy `examples/clusters/` into it only if you also
intend to contribute the profile upstream as a starter alongside
`dartmouth`/`unity`/`sherlock`.

1. Copy the closest starter: `cp examples/clusters/dartmouth.yaml ~/config/your-site.yaml`.
2. Edit `script_preamble`:
   - keep the `source "{{MECHABABS_VENV}}/bin/activate"` line exactly as-is,
   - set `JOB_TMP` to your scratch root,
   - add any `module load` / `PATH` lines your site needs (see `unity.yaml`).
3. Set `job_compute_space` to your scratch base.
4. If you'll run fmriprep/mriqc, point the templateflow / FS-license binds in those
   `examples/pipelines/*.yaml` at your site's paths (the gap above).
5. Your profile does not have to be committed anywhere: `test-cluster --cluster` reads
   the config from the path you hand it, and `configure` copies it into the campaign.
   (Validating from a *checkout* does require your mechababs work to be committed, since
   the dev campaign clones your branch — but that is about the code under test, not your
   cluster profile.)

## Validate by running the e2e on your cluster

Run this on a **login node** — the cluster is the substrate, so there is no
container here.

### From a campaign

If you have a bootstrapped campaign, validate from it (under `tmux`/`screen` — a
login-node disconnect kills the run):

```bash
cd my-campaign
source .venv/bin/activate
mechababs test-cluster --cluster ~/config/your-site.yaml
```

That is the whole setup. The campaign already carries the pinned babs, an isolated
venv with the scenario in it, and a workdir, so there is nothing to export and no
checkout to make. Pass the **path** to your config, as above: a bare name is resolved
against the campaign's own `clusters/`, which `configure` populates — so the name form
only works after you have configured, and validating first is the point. Arguments after
a literal `--` pass through to pytest
(`mechababs test-cluster --cluster ~/config/your-site.yaml -- -k test_full_run`).
The container shim remains the one prerequisite — see [installation.md](installation.md).

**It does not run in the campaign you point at.** The scenario configures a campaign,
registers a dataset, and retires a derivative, so it builds its *own* throwaway
campaign — provisioned from your campaign's pins, so the tools under test are the ones
your campaign records. Your campaign supplies the environment, not the workspace.

### From a checkout

Developing mechababs itself, you take the *same two steps* a user does — bootstrap a
campaign, then validate from it — with your checkout as the mechababs pin instead of a
released ref. `run_on_cluster.sh` does both:

```bash
export MECHABABS_E2E_WORKDIR=/your/cluster/scratch
./mechababs/testing/e2e/run_on_cluster.sh ~/config/your-site.yaml
# a single test:
./mechababs/testing/e2e/run_on_cluster.sh ~/config/your-site.yaml -- -k test_full_run
```

which is this, by hand:

```bash
# a fresh path each run — bootstrap refuses one that exists
DEV=$MECHABABS_E2E_WORKDIR/dev-campaign-$$
./bootstrap.sh "$DEV" \
    --mechababs "$PWD@$(git symbolic-ref --short HEAD)" \
    ${BABS_SPEC:+--babs "$BABS_SPEC"}
cd "$DEV"
source .venv/bin/activate
mechababs test-cluster --cluster ~/config/your-site.yaml
```

Get the prerequisites in place first — see [installation.md](installation.md): the PATH
tools (`git` and `uv` for bootstrap, apptainer/singularity for the jobs), a scratch
workspace and `MECHABABS_E2E_WORKDIR`, and the container shim. The campaign venv
bootstrap builds is what runs the scenario, so there is no separate driver venv to make.

Because bootstrap **clones** your branch into the campaign, only committed work is under
test — the wrapper refuses a dirty tree rather than quietly validate your last commit.

By default babs is pinned to `PennLINC/babs@main`; set `BABS_SPEC=<url@ref>` (a public
https URL the campaign clones anonymously) if your run needs an unmerged babs branch. The
wrapper forwards it to bootstrap as `--babs`, so the dev campaign pins it and the
scenario's own campaign inherits it from those pins. Nothing else reads the variable —
by hand, pass `--babs` yourself, as above.

`run_on_cluster.sh` is a thin wrapper: it guards the environment contract above (workdir
set, shim built, site-packages unset, tmux, a clonable ref) and hands off to
`test-cluster`. Every site runs the *same* command with its own config — per-site
differences belong in your cluster YAML, never in how you invoke this.

**What a green run means:** the suite bootstrapped a campaign, configured it with
*your* cluster profile, submitted real SLURM jobs, waited on them, merged, and
asserted a produced derivative landed in the output RIA. If it passes, your cluster
config produces derivatives — you're ready to point mechababs at real datasets.

## See also

- [reference.md](reference.md) — the config files and the rest of the CLI.
- [CONTRIBUTORS.md](../CONTRIBUTORS.md) — developing and testing mechababs itself,
  including the much-faster local-container test rung.
