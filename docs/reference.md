# mechababs — CLI & workflow reference

The conceptual model (the three axes, the campaign, the reconciler tick) is in
[overview.md](overview.md); recovering from failures and changing a running
campaign are in [interventions.md](interventions.md). This is the operational
reference: the CLI, selection, and the config files.

## CLI — two layers

Bootstrapping has a chicken-and-egg problem: the operate-side CLI is installed
*from the mechababs code vendored into the campaign*, so it can't be what creates
the campaign. The split is bootstrap-vs-operate:

### 1. `bootstrap.sh` — build the environment (run once per campaign)

`bootstrap.sh` is self-contained — it clones the code pins itself, so you only
need `git` + `uv` on PATH; no repo checkout required. Pipe it straight into bash:

```bash
curl -sSL https://raw.githubusercontent.com/con/mechababs/main/bootstrap.sh \
  | bash -s -- my-campaign \
      [--babs URL@REF]        # default: https://github.com/PennLINC/babs.git@main
      [--mechababs URL@REF]   # default: https://github.com/con/mechababs.git@main
```

Clones the two code pins into `code/`, builds `.venv` with `uv`, makes the
directory a datalad dataset (`datalad create --force` over the populated tree),
registers the code clones as subdatasets, and editable-installs the pinned
`babs` + `mechababs` + campaign extras (`requirements-campaign.txt`) into the
venv. Afterwards the pinned tools run *by construction*.

`REF` must be a branch or tag name (not a bare commit sha) — `git clone --branch`
is how the pin is set.

### 2. `mechababs {configure,add-dataset,iterate,status,retire-derivative}` — operate (run from the campaign venv)

```bash
cd my-campaign
source .venv/bin/activate

# bind an ordered pipeline-set to a cluster: copy the named configs into the
# campaign's pipelines/ + clusters/, vendor containers, write the mechababs config
# + the empty ledger. Guards that THIS process is the campaign venv's python (the
# check that prevents the wrong, unpinned babs from running).
mechababs configure \
    --pipelines code/mechababs/examples/pipelines/MRIQC-24.0.2.yaml \
    --cluster code/mechababs/examples/clusters/dartmouth.yaml \
    [--limit N]              # cap each dataset's inclusion to the first N eligible subjects

# register a dataset by URL (append a ledger row). Derives processing_level
# from OpenNeuroStudies metadata (has-sessions -> session).
mechababs add-dataset https://github.com/OpenNeuroDatasets/ds005896

# advance the campaign one reconciler tick (see below)
mechababs iterate [--batch N] [--dry-run]

# read-only: one row per job across every (dataset, pipeline) cell —
# dataset · pipeline · sub/ses · job_id · state · time_used/limit · failed · log path
mechababs status [-o columns|tsv|vd]     # default: an aligned table
                 [--study ds004044] [--derivative MRIQC-24.0.2] [--failed]
                 [--no-refresh]          # skip the per-cell `babs status` refresh

# retire a derivative that has to be redone: move it out of its study into
# derivative-attempts/ and reset its ledger cell, so iterate re-scaffolds it
mechababs retire-derivative studies/study-ds004044/derivatives/fMRIPrep-25.2.5+minimal
                 [--dry-run]
```

`retire-derivative` exists because a cell that must be redone (a resource change, a
tool bug, a config fix) leaves a derivative that is no longer wanted in the study but
is still worth keeping — its logs, git history, and `datalad run` records are the
evidence for *why* it was redone. It moves the dataset to
`derivative-attempts/<dataset_id>-<derivative>-attempt-<N>` (the dataset prefix
avoids a path collision between two datasets retiring the same pipeline; `attempt-N`
covers the same cell being retired twice) and **resets the ledger cell in the same
transition** — so there is no window where the derivative is gone but the cell is
still routed as in-progress, and no hand-edit to forget. The move preserves the
dataset's `datalad-id`, so it is the same dataset relocated, not a copy.

**A retired derivative is an archive, not a resumable babs project.** babs bakes
absolute RIA paths in at init (the same reason babs projects can't be relocated), so
after the move its `input`/`output` siblings still point at the old
`studies/study-<id>/derivatives/<name>/.babs/…` path. No data is lost — the RIA
stores live under `.babs/` and move with it — but those references dangle, so **babs
commands won't operate on it, and neither will `datalad get`/`push` through those
siblings**. Read its logs, history and content; retire a cell you mean to redo from
scratch, not one you mean to continue.

`status` aggregates each babs project's `code/job_status.csv` (which carries no
dataset/pipeline column, and where every job is named `bid`) so a failure points
straight at its log. It refreshes each matched cell from `sacct` first — `--study`
/`--derivative` narrow *before* the refresh, so scoping keeps it fast — and
`--no-refresh` makes reading the possibly-stale cache an explicit choice.

`configure` refuses to overwrite an existing ledger — resetting a campaign is
"delete `desc-mechababs_datasets.tsv`, re-run `configure`" (containers already
vendored are reused).

## Selection & inclusion

Selection lives on the **pipeline axis**, generated per `(dataset, pipeline)` at
deploy time — the only point where both axes (and, for downstream stages,
upstream passers) are known. `mechababs/select.py` fetches the OpenNeuroStudies
per-study TSV (which carries per-subject `datatypes` / `t1w_num` / `bold_num`, so
no clone or annex content is needed) and applies the pipeline's eligibility rule:

| Pipeline | Rule |
|---|---|
| `mriqc` | `'anat' in datatypes` AND `t1w_num > 0` |
| `fmriprep` | anat + func present AND `t1w_num > 0` AND `bold_num > 0` |

The eligible list is sorted and truncated to `--limit N` (a reproducible
"first N"), formatted to match the ledger's `processing_level` (`sub_id` vs
`sub_id,ses_id`), and passed to `babs init --list-sub-file`, which defines the
job *universe*. babs inner-joins it with the subjects actually present in the
data and records that as its own `processing_inclusion.csv` inside the derivative;
mechababs also pins the requested list on the *campaign* at
`.mechababs/inclusions/<dataset>_<pipeline>.csv` (orchestration provenance lives
where mechababs is pinned) as a diagnostic record of intent — its diff against
babs's `processing_inclusion.csv` catches a selected subject the data doesn't have.

For a smoke test, hand-write a one-row inclusion and drop it at the pin path;
`iterate` uses a present pin as-is (no `select`):

```bash
mkdir -p .mechababs/inclusions
printf "sub_id\nsub-CSI1\n" > .mechababs/inclusions/dsXXXXXX_<short>.csv
mechababs iterate --batch 1
```

Or, for a pass-through pipeline (`selection: {}`), cap the job universe at
`configure` time with `--limit 1` and let `iterate` generate the inclusion.

## Responding to failures & applying changes

When a cell fails for a reason a human has to fix, or you need to change an
already-scaffolded cell, see [interventions.md](interventions.md) — repairing a
derivative in place, and updating the pinned code.

## Manual shims (temporary, automated later)

Steps done by hand for now, each with a matching `TODO(manual step):` comment at
the code location that needs it (grep `TODO(manual step)`):

- **Container shim.** Vanilla babs locates a container image at babs's hardcoded
  default path; the ReproNim/containers layout is only resolved by a babs fork
  branch. So `tmp-repronim-container-shim.sh` builds a persistent,
  out-of-campaign shim dataset once (a sibling of your campaigns); pipelines
  reference it relatively (`container.source: ../repronim-containers-shim`) and
  `configure` vendors it into `code/`. Drop the shim and swap `container.source`
  to the ReproNim GitHub link when `PennLINC/babs#383` lands.

## Configuration

Configs are **campaign-owned**: they live in the campaign's own `pipelines/` and
`clusters/`, so the config that produced a run is committed alongside it. `configure`
copies the config you name into the campaign and resolves it by name (a name already
there resolves in place). The files under `examples/` are starters to copy from.

- **Pipeline YAMLs** hold the `container` block, BIDS-app flags, and zip
  foldernames; the filename stem is the pipeline's identity (the ledger column
  prefix, unique per campaign — there is no `short_name` key). Flags are the ground
  truth for what runs — the *why* behind each choice lives in the
  `OpenNeuroDerivatives/fmriprepDerivatives` opinions repo.
- **Cluster YAMLs** — how to activate the campaign venv and where per-job scratch
  lives; see the
  [cluster config & testing tutorial](cluster-config-and-testing-tutorial.md) for
  the full walk-through of adding your own cluster.

To add a pipeline: copy an existing `examples/pipelines/*.yaml` to a
uniquely-named file (its stem is the pipeline's identity), change container + flags.
To add a cluster: copy
`examples/clusters/dartmouth.yaml`, adjust the `script_preamble` + scratch roots,
and validate it by running the e2e suite on your cluster (see the tutorial).
