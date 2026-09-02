# mechababs — contributor conventions

The user-facing docs are included here so a fresh session reads them as ground truth: the words, the concepts, the CLI, recovery, and the on-disk layout.
The README is the landing page; the quickstart is the first run.

@docs/glossary.md

@docs/overview.md

@docs/reference.md

@docs/interventions.md

@docs/output_structure.md

The rest of this file holds the conventions a contributor (or fresh Claude session) needs that the docs don't carry.

## The docs and the spec are the contract

The docs describe the tool as it is; `docs/spec.md` holds the design decisions of record, `docs/use_cases.md` the user stories they answer to, and `docs/output_structure.md` the resulting layout.
If code and any of them disagree, fix the drift in the same change: a changed decision updates the spec, a changed command updates the reference, a changed layout updates the structure doc, and a new word goes in the glossary.
Where today's output deviates from the structure doc, there is an open issue for the gap, or the doc is wrong and changes.
Don't narrate the transition in a doc ("this used to be X"); write the current shape.

## Conventions

- **Wrap runs in duct.** Every run should carry con/duct usage/resource logs alongside its outputs; `con-duct` is a dependency of every campaign environment.
  The wiring is tracked, not yet built: duct-wrapping each `iterate` step is #53, and duct inside the babs jobs is #16 (tracks `PennLINC/babs#356`).
- **No untracked-local paths in upstream-facing stuff** (issues, tracked docs).
  A gitignored path means nothing to a reader on GitHub — **strip the path, keep the intent** (e.g. "the resample question in our fmriprep meeting notes is stale", not the path); remove it at filing time.
- **Configs are the user's, never the tool's.** `examples/` are starters to copy; real site paths never land there, and nothing in the package resolves a config by name.
  Our own deployment's configs live outside this repo.
- **New markdown is one sentence per line, unwrapped.** Don't rewrap existing text just to apply this.

## Planning & issue tracking

Issue discipline: few, closeable issues; fuzzy ideas stay out of the milestone plan (label `fuzzy/slop`, no milestone) rather than being drafted privately and re-done; we iterate in public.

### Milestones

Capability-focused, not date-based.
Referred to by full name (`M4-E2E-Automation`), never bare `M4`.
"All OpenNeuro processed" is the **north star** these enable, tracked operationally rather than as a milestone.

- **M1-Shakeout** — *done.* mechababs can run the 1-subject sweep across the priority list.
- **M2-Correct-Publishable** — successful datasets produce **publishable** output.
  Litmus: *any issue that, if unfixed, would force a passing dataset to be redone* (provenance, license, BIDS validity, `dataset_description`, defacing, zip-breaks-provenance).
  Datasets may fail here — that's fine; the ones that succeed are publishable.
  Retries are M4.
  Provenance must be **re-executable**: the `singularity run` command lands in git *and* must re-run on other systems — abspaths in the run record break this.
- **M3-Hard-Datasets** — dataset-specific handling that **doesn't affect output correctness** (giant ~1k-subject → subdataset-per-subject; odd structures needing special handling to run at all).
  Same output, different handling.
- **M4-E2E-Automation** — a launched chunk runs init→submit→merge→record end-to-end, with **retries** + machine-readable done-detection.
  Launching stays manual / in chunks, by design.

**Milestones attach only to mechababs-tracked issues.**
A pure-upstream issue (filed only in `PennLINC/babs`) gets no milestone; to track upstream work in a milestone, file a mechababs issue that references the upstream `#N` (label `babs-upstream`).
The upstream issue does the fixing; the mechababs issue tracks it.
Per-milestone **epics** aggregate the upstream deps as a checklist (#38 = M2, #39 = M4).

### Labels

- `dataset` — a specific-dataset failure/quirk. Every dataset that fails gets one, so a `dataset`-label scan surfaces them all.
- `pipeline:fmriprep`, `pipeline:mriqc` — which app.
- `automation` — the deployment glue (deploy pattern, statefile, scripts).
- `decision` — a science/policy call (e.g. defacing gate, subject-vs-session).
- `epic` — a parent tracking issue (checklist); used for the per-milestone upstream-deps epics above.
- `blocked` — waiting on something (say what, in-issue).
- `generalize` — removes an assumption that only holds for us (OpenNeuro data, a single cluster, our one pipeline) so an outside user can compose their own dataset × app × cluster.
  A cross-cutting *why* facet, not a work-type — pair it with `automation`/`decision`/etc.
- `fuzzy/slop` — an exploratory / not-fully-baked idea we still want in the tracker so it isn't lost, but that hasn't earned a milestone.
  Files to mechababs, no milestone.
  Promote (drop the label, add a milestone) when it sharpens.

**Upstream-tracking labels** — fixes that land in a repo we don't own; repo-pointer + status:

- `babs-upstream` — fix lands in `PennLINC/babs`; carry the upstream `#N`.
- `upstream` — generic pointer for a **non-babs** upstream (con/duct, fmriprep, datalad, OpenNeuro, …); pair with a more specific label where one exists.
- `upstream-NOT-FILED` — the upstream issue hasn't been filed yet.
- `duct` — touches `con/duct`.
- `fmriprepDerivatives` — belongs in `OpenNeuroDerivatives/fmriprepDerivatives` (the opinions repo).

## Principles

The [STAMPED paper](https://github.com/stamped-principles/stamped-paper) should inform all design and implementation decisions.
When in doubt, ask: does this make the research object more **S**elf-contained, **T**racked, **A**ctionable, **M**odular, **P**ortable, **E**phemeral, and **D**istributable?

## Babs source

mechababs targets **vanilla babs `main`** by default (`PennLINC/babs`, or a PR branch under test), and can point at a fork when one is needed — but a fork is a liability we'd rather not carry, so prefer pushing what we need upstream.
A campaign pins the chosen ref with `campaign init --babs URL@REF`; the released babs predates fixes mechababs depends on, so real runs pin `main` or a branch.

## Where to read in

Start with the [quickstart](docs/quickstart.md), then the docs imported above.
For a design question: `docs/spec.md`, then the use case it traces to.
For the flags an app runs with: the campaign's own app configs are ground truth, `examples/bids-app-configs/` the generic cut, and the `OpenNeuroDerivatives/fmriprepDerivatives` opinions repo the rationale.
For current work and open issues: the GitHub tracker.
