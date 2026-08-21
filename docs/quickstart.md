# mechababs quickstart

> 🚧 **Aspirational** — the study-first UX we're building toward; not all of it runs today.

## Prerequisites

`uv`, `git`, `datalad`, `apptainer`/`singularity`, `git-annex`.
HPC setup — scratch dirs, caches, and especially `git-annex` (a system binary `uv` won't install) — is in [installation.md](installation.md).

You never install mechababs globally, and you never invent syntax — it's plain `uv`.

## Set up (once)

The first campaign is created by running mechababs straight from a pinned ref via `uvx` — nothing lands on your `PATH`, nothing is pre-installed.
App and cluster configs are **your own**, given by path or URL; `examples/` in the mechababs repo are starters to copy and adapt.

**You have a study:**
```bash
cd study-ds000001
uvx --from git+https://github.com/con/mechababs@v0.2 mechababs campaign init nprep \
    --apps mriqc.yaml,fmriprep-anat.yaml,fmriprep-minimal.yaml --cluster dartmouth.yaml
```

**You don't — scaffold a superstudy to hold many:**
```bash
uvx --from git+https://github.com/con/mechababs@v0.2 mechababs campaign init nprep \
    --superstudy my-lab-studies \
    --apps mriqc.yaml,fmriprep-anat.yaml,fmriprep-minimal.yaml --cluster dartmouth.yaml
cd my-lab-studies
```

`campaign init` writes `.mechababs/campaigns/nprep/` — the configs copied in, plus a `pyproject.toml` + `uv.lock` pinning the exact `mechababs` + `babs` (by version number for a released version, by commit for a git source) — and builds the campaign's venv from that lock.
**The lock is your provenance, captured just in time.**
`fmriprep-minimal` declares in its app config that it depends on `fmriprep-anat`; the chain runs in order.

## Daily use

Once a campaign exists, mechababs runs from its venv — `uvx` was only the first step.
Each campaign has an `env.sh` that selects it and activates its venv in one step:
```bash
source .mechababs/campaigns/nprep/env.sh
```
The selection `env.sh` exports (`MECHABABS_CAMPAIGN`) is what names the campaign you're operating on — always explicit, whether the study has one campaign or five.
mechababs refuses to act if the running environment doesn't match the campaign's committed lock, so you can't run the wrong tools by accident.

## Add data

A campaign acts on the source datasets you explicitly select.
In a study, select a source dataset **already present** in it — the enclosing study is found by walking up from the path:
```bash
mechababs add-dataset --sourcedata sourcedata/ds000001
```
At a superstudy, `--study <url>` first clones the member study in, then selects the source dataset inside it:
```bash
mechababs add-dataset --study https://github.com/OpenNeuroStudies/study-ds000001 \
    --sourcedata sourcedata/ds000001
```
One verb covers every shape — a dataset in your lone study, another dataset of a member already added, or a whole study brought in to select a dataset inside it.

## Run it

`iterate` is one reconciler tick — scaffold → submit → merge, each cell advancing as far as it can.
Repeat until everything is merged.
```bash
mechababs iterate
mechababs status
```
A campaign is operated from the level where it was configured — the superstudy for a superstudy campaign, the study for a study campaign.
At a superstudy, `iterate --study study-ds000001` narrows the tick to one member, to concentrate resources on finishing it.

## What you get

Each derivative lands in its study's `derivatives/`, standalone and reproducible; the study's git history carries the `datalad run` records of how it was orchestrated.
Publish outward when ready.
