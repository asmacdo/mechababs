# mechababs — cluster config & testing tutorial

Bringing mechababs to a new HPC is two steps: write one small **cluster config**,
then **validate it by running the real e2e suite on your cluster**. That second
step is a stronger check than `babs check-setup` — it drives the whole spine
(`campaign init` → `add-dataset` → `iterate`: scaffold → submit → wait → merge) in a
throwaway study and asserts a real derivative landed, so it catches HPC-specific
breakage a scaffold-only deploy would miss.

## What a cluster config is

A cluster config is small. It answers **how to enter the campaign environment**,
**where per-job scratch lives**, and — only where the site needs it — **which package
versions the site can install**. Here is the bundled `examples/clusters/dartmouth.yaml`,
minus its commented-out `env_constraints` starter:

```yaml
script_preamble: |
  # campaign venv; mechababs substitutes the placeholder with the real path when it composes the babs config
  source "{{MECHABABS_VENV}}/bin/activate"
  export JOB_TMP="/scratch/${USER}/sjob-tmp/${SLURM_ARRAY_JOB_ID}_${SLURM_ARRAY_TASK_ID}"
  mkdir -p "${JOB_TMP}"
  trap 'rm -rf "${JOB_TMP}"' EXIT

job_compute_space: "/scratch/${USER}"
```

- **`script_preamble`** — shell that runs at the top of every job: activate the
  campaign venv (via the `{{MECHABABS_VENV}}` placeholder, which mechababs
  substitutes with the campaign venv's real path when it composes the babs config;
  leave it literally as written), set a per-job `JOB_TMP` under your scratch, and clean it up on exit.
- **`job_compute_space`** — the scratch base the job works in.
- **`env_constraints`** (optional) — version caps for the campaign environment, as verbatim PEP 508 specifiers.
  They become uv `constraint-dependencies`: they cap a package the resolution already contains and never add one, so the campaign's own dependency floors are preserved.
  Leave the key out on a modern cluster.
  It exists for a site whose glibc is older than the newest manylinux wheels target — CentOS 7's 2.17 is still common on HPC — where uv otherwise falls back to source builds the site cannot compile and `campaign init` dies in compiler output.
  `examples/clusters/sherlock.yaml` carries the glibc-2.17 set live; `dartmouth.yaml` carries it commented, as a starter.

**What is *not* here (a common misconception):** SLURM resources
(`cluster_resources`) and the container's `-B $JOB_TMP:/tmp` bind live in the
**app configs**, not the cluster file. The cluster config only *supplies* `$JOB_TMP`
(via the preamble) and `job_compute_space`; the app configs consume `$JOB_TMP`. So
"how big/long a job is" is app config; "where scratch is and how to enter the env" is
cluster config.

`examples/clusters/unity.yaml` is the best real-world adaptation to read: Unity ships no
git-annex on the compute nodes, so its preamble prepends a workspace-local
git-annex build to `PATH`, and it roots scratch under an allocated HPC workspace
(`/scratch4/workspace/${USER}-mechababs`) because Unity has no persistent per-user
`/scratch`. Same two keys, site-specific values.

## Known gap: some site config still lives in the app configs

One honest caveat before you start, and the config-decoupling work we would most like
help with: templateflow and the FreeSurfer license are bind-mounted from paths inside
the fmriprep and mriqc app configs. A new site edits those lines, marked `SITE` in the
starters, in the app configs it uses, not just the cluster file. By rights a site path
belongs on the cluster axis; today it does not. (The SimBIDS starter has no such binds,
so the validation below is unaffected; a real fmriprep run needs the edit.)

Cluster and app configs themselves are **campaign-owned**: `campaign init` copies the
configs you name into the campaign's own `clusters/` and `bids-app-configs/`, so the
config that produced a run is committed alongside it. They are always given **by
path or URL**, never by a name the tool looks up: the files under `examples/` are
starters to copy from, not a directory mechababs resolves against, so using mechababs
at your site needs no fork of it.

## Add your cluster

Write your config wherever you keep site config and pass its path — both
`test-cluster --cluster` and `campaign init --cluster` take one, so nothing has to
live in a checkout. Copy `examples/clusters/` into it only if you also intend to
contribute the config upstream as a starter alongside
`dartmouth`/`unity`/`sherlock`.

1. Copy the closest starter: `cp examples/clusters/dartmouth.yaml ~/config/your-site.yaml`.
2. Edit `script_preamble`:
   - keep the `source "{{MECHABABS_VENV}}/bin/activate"` line exactly as-is,
   - set `JOB_TMP` to your scratch root,
   - add any `module load` / `PATH` lines your site needs (see `unity.yaml`).
3. Set `job_compute_space` to your scratch base.
3.1. Leave `env_constraints` out unless the environment build fails.
   If it does, the error names the package with no installable wheel — add a cap for it under `env_constraints` and start the campaign again.
   On a glibc-2.17 site, start from `sherlock.yaml`'s block rather than discovering the eight one at a time.
4. If you'll run fmriprep/mriqc, point the templateflow / FS-license binds in those
   `examples/bids-app-configs/*.yaml` at your site's paths (the `SITE` lines; the gap above).
5. Your config does not have to be committed anywhere: `test-cluster --cluster` reads
   the config from the path you hand it, and `campaign init` copies it into the campaign
   when you go on to use it for real.

## Validate by running the e2e on your cluster

Run this on a **login node**, under `tmux`/`screen` — the cluster is the substrate, so
there is no container here, and a login-node disconnect kills the run.

```bash
uvx --from 'git+https://github.com/con/mechababs@main#egg=mechababs[test]' \
    mechababs test-cluster \
    --cluster ~/config/your-site.yaml \
    --scratch-path /your/cluster/scratch
```

That is the whole setup — no checkout, no venv to build, nothing to export.
Arguments after a literal `--` pass through to pytest
(`… --scratch-path /scratch -- -k test_spine`).

Three things about that command are load-bearing:

- **`--cluster` takes a path.** Configs are user-provided; there is no directory
  mechababs resolves a bare name against.
- **`--scratch-path` is required**, and belongs on fast cluster scratch. The fixture
  studies, the container dataset they resolve as their sibling, and the caches all
  live there — never home or `/tmp`.
- **The `[test]` extra is not optional.** The scenario runs as
  `sys.executable -m pytest`, so pytest has to be installed beside the mechababs you
  invoke. It cannot come from the campaign the scenario builds, because building that
  campaign is the scenario's first step.

**It never touches a real study.** A campaign lives *inside* a study, so there is
nothing standalone to point this at: the scenario fabricates its own study under the
scratch path and runs the real spine — `campaign init` → `add-dataset` → `iterate`
(scaffold → submit → merge) — in that.

**It validates the mechababs you invoked.** With `--mechababs` unset, `campaign init`
pins whichever mechababs is running the command, exactly as it would for a user's own
campaign — so the ref in the `uvx --from` above is the code under test, and testing a
branch is a matter of changing it. babs is different: it is not a mechababs dependency
but a dependency of the *generated campaign*, frozen by that campaign's lock, so the
fixture campaign gets what a user's campaign would get. `--babs <url@ref>` overrides
that, which is how an unmerged babs fix gets run through the scenario.

### Developing mechababs itself

An unpushed branch is testable the same way: `uvx --from` takes anything `pip` can
install, and `--mechababs` takes anything `git clone` accepts — a path on the cluster
filesystem included. There is no separate dev route into the scenario; dev and prod
differ only in the *value* of those pins.

The much faster local rung — the same scenario under rootless podman against a
container running SLURM — is in [CONTRIBUTORS.md](../CONTRIBUTORS.md).

**What a green run means:** the suite built a campaign against *your* cluster config,
resolved and installed its environment, submitted real SLURM jobs, waited on them,
merged, and asserted a produced derivative landed. If it passes, your cluster config
produces derivatives — you're ready to point mechababs at a real study.

## See also

- [reference.md](reference.md) — the config files and the rest of the CLI.
- [CONTRIBUTORS.md](../CONTRIBUTORS.md) — developing and testing mechababs itself,
  including the much-faster local-container test rung.
