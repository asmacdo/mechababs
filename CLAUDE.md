# mechababs — contributor conventions

The project overview and the campaign model live in `docs/overview.md`; the CLI,
selection, and per-dataset workflow reference lives in `docs/reference.md`;
recovery/change workflows live in `docs/interventions.md`. They are included here
(the README itself is now just a landing page):

@docs/overview.md

@docs/reference.md

@docs/interventions.md

The rest of this file holds project conventions, terminology, and pointers a
contributor (or fresh Claude session) needs that don't fit in the README.

## OpenNeuro ecosystem

Three GitHub orgs work together:

| Org | Role | Example |
|---|---|---|
| **OpenNeuroDatasets** | Raw BIDS data | `ds005256` |
| **OpenNeuroDerivatives** | Processing outputs | `ds000001-mriqc` |
| **OpenNeuroStudies** | Glue — links raw to derivatives | `study-ds000001` |

`OpenNeuroStudies/OpenNeuroStudies` is a datalad superdataset; each
`study-dsXXXXXX/` subdataset has `sourcedata/dsXXXXXX` linking to
OpenNeuroDatasets and `derivatives/<Pipeline-Ver>/` linking to
OpenNeuroDerivatives. `studies.tsv` (maintained by Yarik) is the
authoritative index — columns include `study_id`, `raw_version`,
`derivative_count`, `derivative_ids`.

The platform itself lives at **`openneuroorg/openneuro`** (web app,
validation, the S3 content buckets). Dataset-level *data* problems are
tracked there, and dataset state is visible on the dashboard:
<https://openneuroorg.github.io/dashboard/>.

**Reporting a dataset's data problem upstream.** When a dataset fails for
a *data* reason — content not pushed to the bucket (annex content
unavailable), missing/invalid files, not yet propagated — report it to
the platform, not just here (per Chris Markiewicz):

1. Check the dataset on the dashboard above.
2. Search the **dataset ID across all** `openneuroorg/openneuro` issues —
   `gh search issues --repo openneuroorg/openneuro "dsXXXXXX"`. Do **not**
   filter to `label:Tracking`: real reports often land under other labels
   (e.g. ds006623 is covered by `openneuroorg/openneuro#3875`, labeled
   `bug`). The `Tracking` label
   (<https://github.com/openneuroorg/openneuro/issues?q=is%3Aissue+state%3Aopen+label%3ATracking>)
   is a useful browse bucket, not a complete filter.
3. If one already covers it, add a comment naming the dataset (if not
   already listed); otherwise open a new issue.

Then link the upstream issue from our `dataset`/`upstream` issue and drop
`upstream-NOT-FILED`. (Tool/config failures — e.g. a pipeline that can't
read a valid file — are *our* issues, not this.)

## Pipeline

mechababs is automation to run **one pipeline** right now — staged fmriprep
with MRIQC as a gate — architected to generalize later but not abstracted
ahead of need. The pipeline *is* the app.

Stages (each a separate BABS run, composed via the three-axis YAMLs):

1. **MRIQC** — quality gate; must pass first.
2. **`fmriprep --anat-only`** — FreeSurfer + anat scaffold + xfms to output spaces.
3. **`fmriprep --level minimal`** — BOLD-side transforms (HMC, coreg, SDC).
4. **`fmriprep --level resampling`** — currently equivalent to minimal in
   fmriprep; kept as a no-op hook for the planned confounds-at-resampling
   change (#17).
5. **`fmriprep --level full`** — resampled BOLD in template spaces + CIFTI +
   confounds. More expensive than minimal — it resamples every volume into each
   output space and projects to surfaces — but the ratio has never been
   measured here, so don't quote one.

**Fan-out, not chain.** Stages 3–5 each take anat-only's output as a
`sourcedata/` input; they do *not* chain off each other (BABS is single-input
per derivative). A true linear chain needs upstream BABS work — tracked in
#27. Accepted as fan-out for now.

**Don't restate flags or rationale here — they drift.** Exact flags live in
`examples/pipelines/fMRIPrep-*.yaml` (ground truth); the *why* behind each choice
(version pin, me-output-echos hedge, syn-sdc, slice-timing) lives in the
`OpenNeuroDerivatives/fmriprepDerivatives` opinions repo.

## Target structure

@docs/output_structure.md

We should be producing things according to the stucture document.
Any deviations should be open issues or else the document itself needs to change.

## Conventions

The README covers three-axis composition, one-tool-two-modes / dev-exercises-prod,
and the selection & inclusion flow. These are the conventions it doesn't:

- **Wrap runs in duct.** Every run should carry con/duct usage/resource
  logs alongside its outputs — con/duct is a first-class campaign tool
  (installed into the venv, PATH-checked by `iterate`). The campaign
  wiring is tracked, not yet built: duct-wrapping each `iterate` step is
  #53, and duct inside the babs jobs is #16 (tracks `PennLINC/babs#356`).
- **Curated facts live in `priority-openneuro-datasets.csv`.** It's the
  human-edited list of datasets we care about. Don't synthesize a parallel
  source; add columns here if a per-dataset fact needs to be tracked.
- **No untracked-local paths in upstream-facing stuff** (issues, tracked
  docs). A gitignored path means nothing to a reader on GitHub — **strip
  the path, keep the intent** (e.g. "the resample question in our fmriprep
  meeting notes is stale", not the path); remove it at filing time.
- **Dataset failures → always a mechababs issue, `dataset`-labeled.** Every
  dataset that fails (data fault / won't process) gets a mechababs issue with
  the `dataset` label, so failures are milestone-tracked and a `dataset`-label
  scan after a shakeout surfaces them all. Put the dataset ID in the title for
  single-/few-dataset issues; for one root cause hitting many datasets, keep the
  IDs in a body checklist (don't cram them into the title) — the `dataset` label
  is what makes it scannable, so a multi-dataset root-cause issue carries
  `dataset` even when the cause is ours/upstream. If the cause is upstream,
  **also file upstream and link it** (see the OpenNeuro reporting workflow
  above), don't just point at it; default for data problems is alert-upstream,
  not self-fix (case-by-case). Per-dataset shakeout *status* still lives in the
  operational ledger — issues are the failures/causes, not a card per dataset.

## Planning & issue tracking

Issue discipline: few, closeable issues; fuzzy ideas stay out of the
milestone plan (label `fuzzy/slop`, no milestone) rather than being
drafted privately and re-done; we iterate in public.

### Milestones

Capability-focused, not date-based. Referred to by full name
(`M4-E2E-Automation`), never bare `M4`. "All OpenNeuro processed" is the
**north star** these enable — tracked by the operational ledger, not a
milestone.

- **M1-Shakeout** — *done.* mechababs can run the 1-subject sweep across
  the priority list. The ongoing sweep is an activity (the ledger), not a
  bucket.
- **M2-Correct-Publishable** — successful datasets produce **publishable**
  output. Litmus: *any issue that, if unfixed, would force a passing
  dataset to be redone* (provenance, license, BIDS validity,
  `dataset_description`, defacing, zip-breaks-provenance). Datasets may
  fail here — that's fine; the ones that succeed are publishable. Retries
  are M4. Provenance must be **re-executable**: the `singularity run`
  command lands in git *and* must re-run on other systems — abspaths in
  the run record break this.
- **M3-Hard-Datasets** — dataset-specific handling that **doesn't affect
  output correctness** (giant ~1k-subject → subdataset-per-subject; odd
  structures needing special handling to run at all). Same output,
  different handling.
- **M4-E2E-Automation** — a launched chunk runs init→submit→merge→record
  end-to-end, with **retries** + machine-readable done-detection.
  Launching stays manual / in chunks, by design.

**Milestones attach only to mechababs-tracked issues.** A pure-upstream
issue (filed only in `PennLINC/babs`) gets no milestone; to track upstream
work in a milestone, file a mechababs issue that references the upstream
`#N` (label `babs-upstream`). The upstream issue does the fixing; the
mechababs issue tracks it. Per-milestone **epics** aggregate the upstream
deps as a checklist (#38 = M2, #39 = M4).

### Labels

- `dataset` — a specific-dataset failure/quirk.
- `pipeline:fmriprep`, `pipeline:mriqc` — which pipeline.
- `automation` — the deployment glue (deploy pattern, ledger, scripts).
- `decision` — a science/policy call (e.g. defacing gate, subject-vs-session).
- `epic` — a parent tracking issue (checklist); used for the per-milestone
  upstream-deps epics above.
- `blocked` — waiting on something (say what, in-issue).
- `fuzzy/slop` — an exploratory / not-fully-baked idea we still want in the
  tracker so it isn't lost, but that hasn't earned a milestone. Files to
  mechababs, no milestone. Promote (drop the label, add a milestone) when it
  sharpens.

**Upstream-tracking labels** — fixes that land in a repo we don't own;
repo-pointer + status:

- `babs-upstream` — fix lands in `PennLINC/babs`; carry the upstream `#N`.
- `upstream` — generic pointer for a **non-babs** upstream (con/duct,
  fmriprep, datalad, OpenNeuro, …); pair with a more specific label where
  one exists.
- `upstream-NOT-FILED` — the upstream issue hasn't been filed yet.
- `duct` — touches `con/duct`.
- `fmriprepDerivatives` — belongs in `OpenNeuroDerivatives/fmriprepDerivatives`
  (the opinions repo).

## Principles

The STAMPED paper (`reference/principles-paper/`) should inform all
design and implementation decisions. When in doubt, ask: does this make
the research object more **S**elf-contained, **T**racked, **A**ctionable,
**M**odular, **P**ortable, **E**phemeral, and **D**istributable?

## Babs source

mechababs is an e2e harness for running babs across clusters and many datasets.
It targets **vanilla babs `main`** by default (`PennLINC/babs`, or a PR branch under test), and can point at a fork when one is needed — but a fork is a liability we'd rather not carry, so prefer pushing what we need upstream.
(How a campaign vendors + pins the chosen ref: README, "The campaign".)

## Reference repos

Cloned into `reference/` (gitignored). Before using any reference repo,
**check for upstream updates** (`git -C reference/<repo> pull`).

| Directory | Upstream | Purpose |
|---|---|---|
| `principles-paper/` | https://github.com/myyoda/principles-paper | STAMPED properties paper — the principles guiding this project |
| `OpenNeuroStudies/` | https://github.com/OpenNeuroStudies/OpenNeuroStudies | The superdataset mechababs feeds into |
| `OpenNeuroDerivatives/` | https://github.com/OpenNeuroDerivatives/OpenNeuroDerivatives | Upstream mirrors for derivative datasets |
| `fairly-big-processing-workflow/` | https://github.com/psychoinformatics-de/fairly-big-processing-workflow | The FAIRly Big pattern that BABS implements |
| `containers/` | https://github.com/ReproNim/containers | ReproNim container dataset — archives built SIFs |
| `babs_demo/` | https://github.com/djarecka/babs_demo | Dorota's babs walkthrough (.env-based cluster config). The `*_bids_layout` scripts are the **canonical demonstration of what babs produces**: a derivative dataset inside a study you make yourself. Read these before reasoning about output structure or `analysis_path` |
| `babs-containers-run-test/` | (local, Austin's test scripts) | Reference scripts for testing babs init with containers-run branch |
| `bootstrap_fMRIprep/` | Felix's cerebra.fz-juelich.de gitea | Felix's canonical fmriprep wrapper — reference for opinions repo |
| `ds001761-fmriprep/`, `ds005374-fmriprep/` | OpenNeuroDerivatives mirrors | Joe's published fmriprep runs (2022 + 2025) — reference for output shape |

## Where to read in

For the pipeline: the **`## Pipeline`** section above (shape), the
`examples/pipelines/*.yaml` (flags, ground truth), and the
`OpenNeuroDerivatives/fmriprepDerivatives` opinions repo (rationale).

For current work + open issues: the GitHub tracker.
