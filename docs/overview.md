# mechababs — overview

mechababs is an end-to-end harness for running [BABS](https://github.com/PennLINC/babs) across clusters and many datasets.
It runs **vanilla** BABS by default (`PennLINC/babs` main, or a PR branch under test), and can use a babs fork when one is needed.
The unit of work is a **campaign**: a self-contained datalad dataset that holds its inputs, its outputs, its config, its state ledger, and the exact `babs` + `mechababs` code that produced everything.

## Concept

Every run is the composition of three axes:

- **A dataset** — an OpenNeuro raw BIDS study (`OpenNeuroDatasets/dsXXXXXX`),
  registered by URL (the URL is its identity).
- **A pipeline** — a config in the campaign's `pipelines/` (mriqc, fmriprep-anat /
  minimal / resampling / full, simbids). Holds the container reference + BIDS-app
  flags.
- **A cluster** — a config in the campaign's `clusters/` (`dartmouth.yaml`,
  `test-docker.yaml`). Holds SLURM resources + the job script preamble.

`merge_config.py` composes pipeline × cluster × dataset-URL into the single
`babs-config.yaml` that `babs init` consumes. Never bake cluster details into a
pipeline YAML or vice versa.

## The campaign

A campaign is its **own standalone datalad dataset** — the boundary that makes a
processing run self-contained and reproducible. Its heavy parts (source data,
derivatives, and the vendored code) are subdatasets inside it:

```
my-campaign/                             # a campaign = a datalad dataset (datalad create)
  .mechababs/campaign.yaml               # cluster file + {short_name: pipeline_file} + venv + limit
  clusters/  pipelines/                  # the campaign's own configs (configure copies them in)
  desc-mechababs_datasets.tsv            # the state ledger (one row per dataset)
  .venv/                                 # campaign venv (gitignored, rebuildable)
  code/
    mechababs/                           # subdataset, pinned at a chosen ref
    babs/                                # subdataset, pinned at a chosen ref
    repronim-containers-shim/            # vendored container dataset(s)
  sourcedata/  dsXXXXXX/  …              # subdatasets -> OpenNeuroDatasets
  derivatives/
    dsXXXXXX_mriqc_attempt-1/            # a babs project; attempt-N allocated at creation
    dsXXXXXX_fmriprep-anat_attempt-1/
```

Why this shape:

- **Code is vendored and pinned per campaign.** `code/babs` and `code/mechababs`
  are git submodules; the submodule commit *is* the pin. The campaign venv
  editable-installs them, so the `babs` / `mechababs` that run are the
  provenance-pinned ones recorded in the campaign — not whatever happens to be on
  PATH. A different babs commit (e.g. to test a PR) is just a different pin.
- **State is a re-derivable cache, not the source of truth.** `desc-mechababs_datasets.tsv`
  is reconciled from ground truth (babs / the output RIA) each tick, so a crashed
  run, a hand-edited file, or a changed inclusion self-heals on the next
  `iterate`. To change an outcome, change ground truth (the inclusion, or reset).
- **Outputs are produced and pushed outward** (to OpenNeuroDerivatives /
  OpenNeuroStudies); the campaign is where they're made and tracked, not where
  they permanently live.

**One tool, two modes.** Dev (a scratch sibling, small inclusions, a branch of
babs under test) and production (OpenNeuro siblings, all subjects, released code)
are the *same* tool — every difference is config and content, never a dev-only
branch, field, or code path. Dev exercises prod's exact paths, so dev validates
prod.

**The campaign is a provenance object.** Because everything — inputs, outputs,
pinned code, config, and the ledger — lives under one datalad boundary, the
campaign is positioned to record not just *what* was produced but *how* it was
orchestrated. Today the pinned submodule commits say exactly which `babs` /
`mechababs` ran, and each tick's changes are saved as a grouped, per-transition
commit. The direction (partly built, partly in progress) is for each derivative to
carry a `prov/` record (BEP028-shaped) linking back to the campaign, and for the
orchestration commands to be captured verbatim via `datalad run`. That is the
STAMPED payoff mechababs is built toward — a self-contained, tracked,
re-executable research object — and the reason it sits on top of babs rather than
beside it. See [STAMPED](https://github.com/stamped-principles/) for the principles
and [output_structure.md](output_structure.md) for the target shape.

## The reconciler tick (`iterate`)

`iterate` is one **tick** of a reconciler. It reads the desired state (the ledger
rows) and advances each `(dataset, pipeline)` cell by **at most one transition**,
routing on which ledger columns are populated:

| Cell state | Columns | Transition |
|---|---|---|
| not started | `<short>_babs` empty | **scaffold**: generate the inclusion → compose the babs config → `babs init` (no submit) → pin the inclusion → record `<short>_babs` (the project path) |
| in progress | `<short>_babs` set, `<short>_babs-merged` empty | **active**: read `babs status --json`, decide `submit / skip / merge / flag-failed` from the counts |
| done | `<short>_babs-merged` set | skip (no babs query) |

The active step is decided from `babs status --json` counts: not-all-submitted →
submit; still in flight → skip; all ended with failures → flag (don't merge); all
done → merge. A single writer is enforced by a campaign flock, and each advanced
cell is saved as it lands, so a long or interrupted tick still records progress.
`--dry-run` runs the read-only steps for real and prints the mutating commands
without running them.

There is **no status enum** — a pipeline's state is entirely derived from which
columns are filled. Identity columns (`dataset_id`, `study_url`,
`processing_level`, `n_subjects`, `n_sessions`) are *inputs* iterate reads and
never overwrites; the `<short>_babs*` columns are *derived* and reconciled each tick.

`babs init` runs **on the cluster** (via `iterate`), because babs bakes absolute
RIA-store paths into the project at init that can't be relocated. Cheap steps
(`add-dataset`) can run anywhere; the git-tracked ledger syncs by push/pull while
the heavy RIA stores stay cluster-side.

## Declarative, not imperative — edge- vs level-triggered

You don't tell mechababs *do these steps*. You declare intent — which datasets,
which pipelines, on which cluster — through the CLI (`configure`, `add-dataset`),
and `iterate` reconciles reality toward it, one tick at a time. There are two ways
to drive a system toward a goal. *Edge-triggered*: an event fires and an action
runs in response — a job finishes and kicks off the next one — so a missed event
means permanent drift. *Level-triggered*: a loop repeatedly reconciles reality
toward the declared goal, re-reading ground truth each pass. `iterate` is
**level-triggered** — every tick re-reads ground truth (the babs projects, the
output RIA) and re-derives what each cell needs, regardless of what happened
before. Level-triggered reconciliation is what lets a long, messy, interrupted
campaign converge instead of accumulating drift — the same reason it won out in
cluster orchestration generally.

The interface is the CLI — you don't hand-edit the ledger; mechababs maintains it,
and keeps it deliberately thin. It records the *durable* facts about each cell —
that a babs project exists, and whether it has been merged — and defers the
volatile, moment-to-moment job status to babs, querying it live whenever a cell is
still active. So there is no stale status mirror to drift out of sync: each tick
re-reads the durable markers plus a live babs query and acts on that.

**Self-healing has a deliberate limit.** The *bookkeeping* self-heals; a *failure*
does not. When a job fails for a reason a human has to decide about — an OOM, a bad
flag, a dataset quirk — `iterate` flags the cell and **stops** rather than silently
retrying past it. Recovery is a human act, and the campaign's job is to make it
provenance-safe: the intervention is recorded, not smoothed away (see
[interventions.md](interventions.md)). Messy science is unavoidable; mechababs
captures the mess honestly instead of pretending the run was clean.
