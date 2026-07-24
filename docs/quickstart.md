# mechababs quickstart

> 🚧 **Aspirational** — the study-first UX we're building toward; not all of it runs today.
> The whole document is open for feedback; 💬 marks the points that specifically need it.

## Prerequisites

`uv`, `git`, `datalad`, `apptainer`/`singularity`, `git-annex`.
HPC setup — scratch dirs, caches, and especially `git-annex` (a system binary `uv` won't install) — is in [installation.md](installation.md).

You never install mechababs globally, and you never invent syntax — it's plain `uv`.

## Set up (once)

Bootstrap runs mechababs straight from a pinned ref via `uvx` — nothing lands on your `PATH`, nothing is pre-installed — and writes a pinned environment into your dataset.

**You have a study:**
```bash
cd study-ds000001
uvx --from git+https://github.com/con/mechababs@v0.2 mechababs bootstrap
```

**You don't — scaffold a superstudy to hold many:**
```bash
uvx --from git+https://github.com/con/mechababs@v0.2 mechababs bootstrap --add-superstudy my-lab-studies
cd my-lab-studies
```

Bootstrap writes `.mechababs/` with a `pyproject.toml` + `uv.lock` pinning the exact `mechababs` + `babs` by commit.
**That lock is your provenance, captured just in time.**

## Daily use

Activate the pinned env once per shell — the familiar way:
```bash
source .mechababs/.venv/bin/activate          # or, no activation: uv run --project .mechababs mechababs …
```
mechababs refuses to act if the environment doesn't match the lock, so you can't run the wrong tools by accident.

## Add data (superstudy)

```bash
mechababs add-dataset https://github.com/OpenNeuroDatasets/ds000001
```
💬 `add-dataset` vs `add-study` — and a study can hold **more than one** source dataset, so the verb has to distinguish *adding a new study* (wrapping a dataset) from *adding another source dataset* to an existing study.

## Define a campaign

One run = a bundle of BIDS-App configs + a cluster → writes `.mechababs/campaigns/<label>/`:
```bash
mechababs campaign init nprep --apps mriqc,fmriprep-anat,fmriprep-minimal --cluster dartmouth
```
`fmriprep-minimal` depends on `fmriprep-anat`; the chain runs in order.

## Run it

`iterate` is one reconciler tick — scaffold → submit → merge, each cell advancing as far as it can.
Repeat until everything is merged.
```bash
mechababs iterate        # defaults to the one active campaign; name it if several: mechababs iterate nprep
mechababs status
```
**Where you run it scopes what it touches:** from the superstudy, all members; from inside one study, just that one.

## What you get

Each derivative lands in its study's `derivatives/`, standalone and reproducible, with a `prov/` record of how it was made.
Publish outward when ready.

---

💬 **Open: campaign selection + keeping the environment honest.**
A study accumulates campaigns (config epochs) over time, so "which one am I operating on" has to be answered — the current lean is *derive the single active campaign from ground truth, name it explicitly when several are live* (no stored pointer).
And mechababs must *enforce* that the running tools match the target campaign's pinned lock (refuse on mismatch), so provenance can't silently drift.
The mechanics are unsettled: one study-level mechababs env vs per-campaign; whether babs is pinned per-campaign underneath a single mechababs; multi-user / worktree behavior; and exactly what the guard checks.
Rejected so far: campaign-as-git-branch identity, a `.mechababs/HEAD` file, and an activate-set env var.
Needs discussion.
