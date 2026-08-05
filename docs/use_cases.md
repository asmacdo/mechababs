# mechababs use cases

The user stories mechababs is designed to serve — the requirements the rest of the design answers to.
A mix of what works today and what we are building toward; expected to grow.
When a design decision is unclear, it should be resolvable by asking "which use case does this serve?"

The whole document is open for feedback; 💬 marks the points that specifically need it — open questions we want others to weigh in on.

## Terminology 💬
The words below are used consistently throughout this document and [output_structure.md](output_structure.md); agreeing on them is itself an open question.

- **source dataset** — a raw BIDS dataset, the input to a pipeline.
- **derivative** — what one BIDS App produced from one source dataset.
- **study** — the primary unit: one or more source datasets grouped with the derivatives made from them.
- **superstudy** — a study of studies; an optional layer for operating on many at once.
- **campaign** — one run's pinned environment and bundle of app configs, recorded in each study it touches. Not a dataset.
- **sweep** — running a campaign across the member studies of a superstudy.

A **level** is one of those nested scopes, and the hierarchy is named after a tree: the **leaves** are the derivatives, the **root** is the outermost superstudy.
💬 So "up" means toward the root — which reads backwards for anyone picturing a superstudy sitting on top of its members, and is worth settling before more of the docs lean on it.

## Sweep many pre-made studies
As a mechababs user, I want to operate on ~1000 pre-made BIDS studies that each contain one raw source dataset.
I want to run MRIQC, fMRIPrep `--anat-only`, and fMRIPrep `--level minimal`, where minimal takes its inputs from the anat run.
I want to prioritize finishing and publishing whole datasets over running the first stage of all thousand, so results land incrementally.

## Gate an expensive pipeline on QC
As a user, I want to run a cheap QC app (MRIQC) first and only run the expensive stages (fMRIPrep anat → minimal/full) on the inputs that pass.
The pass/fail verdict is produced outside mechababs, by review or by an automated rule.
What would be helpful is a recorded way to subtract the excluded subjects/sessions from the downstream selection.
This is expressed today by curating the source selection by hand.

## Choose what gets worked next
As a user, I can steer which studies the sweep advances and in what order, rather than taking whatever the reconciler picks up.
Finishing whole studies before starting new ones only pays off if the order is mine to influence.

## Act on one study within a superstudy
As a user, I can direct mechababs at a specific study and have it advance only that one, so I can finish a chunk deliberately instead of spreading progress across the whole set.
💬 Scoping by working directory was the initial pitch, but the superstudy still takes writes when a study finishes — so the working directory may not be the right selector.

## Release a finished study
As a user, once mechababs reports a study finished I can push it and remove it from the cluster.
mechababs neither does that for me nor is disturbed by it: a released study is never brought back.

## See the state of the set without holding the data
As a user, I can tell what is done, in flight, and not started across all member studies without those studies being installed locally.

## Observe the campaign at any level
As a user, I want the same view of progress at every level of the hierarchy — a single derivative, a study, the whole superstudy — so I can zoom out to see how a sweep is going and in to a specific failure without switching tools.

## Trust that a summary agrees with what it summarizes
As a user, I want each level's summary to be derived from the level beneath it rather than maintained alongside it, so the numbers at the top cannot disagree with the detail underneath.
This is the pattern OpenNeuroStudies, AnnexTube, MyKrok and the BIDS inheritance/summarization principle already use — higher-level TSVs produced from lower-level ones — and it is what makes a view of the whole set cheap enough to render.

## Read the state with tools other than mechababs
As a user or tool author, I want the files mechababs keeps its state in to be documented and conventional — BIDS study layout, and [BIDS common conventions on TSV files](https://bids-specification.readthedocs.io/en/stable/common-principles.html#tabular-files) for the tabular ones — so a dashboard, a script, or another lab's tooling can read them without knowing mechababs internals.

## Run a study to completion under a finite budget
As a user with limited disk and inodes, I can sweep more studies than fit at once, because finishing and releasing a study frees the space the next one needs.
💬 Whether a single study's own peak footprint fits is a separate problem, not covered by this.

## Work in a single study, no superstudy
As a researcher with a single BIDS study, I want to run a BIDS App on it without setting up a superstudy.
The study is the thing I operate on; the many-study machinery should stay out of my way.

## Produce a single derivative, easily
As a neuroscientist or student, I want to produce one derivative without caring about the machinery — point at a dataset, pick a pipeline, and go.
Ease is the requirement; the reproducible provenance object should come for free, not as extra work.

mechababs's ease here is *config reuse*, not a lower first-config cost.
If your lab already has the config files, this is easy — point, run, and the configs are shared.
If not, the work is *authoring* those configs, which for a single derivative is about the same as vanilla BABS.
mechababs's win is twofold: it makes those configs shareable and reusable afterward, and it collects the datalad-native orchestration provenance that BABS alone does not — so even the one-off derivative comes out as a self-contained, reproducible object.

If your goal is to *learn* how derivatives are produced rather than to produce one, the [nipoppy](https://nipoppy.readthedocs.io) project (McGill) is designed for exactly that — it teaches the user how to do these things step by step.
mechababs optimizes for producing a self-contained, reproducible object; nipoppy optimizes for teaching the process.
They are complementary.

## Author a study from assorted source datasets
As a mechababs user, I want to create a BIDS study containing a variety of source datasets of different types.
I want to produce derivatives from a variety of BIDS Apps, each across a chosen subset of those source datasets.

This includes a lab with its own (non-OpenNeuro) BIDS datasets running mechababs across several in-house cohorts on its institution's cluster, and getting the same self-contained, reproducible studies + derivatives — without the data being on OpenNeuro or the studies pre-authored by its tooling.
Today each raw dataset is hand-wrapped as a study, along with the per-subject datatypes/counts TSV that selection reads; it works, but it is the main barrier to entry.
This is the demand behind the study-authoring gap in [output_structure.md](output_structure.md): first-class study creation from local raw data.

## Add derivatives to a study later
As a researcher, I want to return to a study I processed a year ago and add a new set of derivatives with newer tool versions.
The earlier derivatives should be left untouched; the new effort records its own environment.

## Clone and extend someone else's study
As a collaborator, I want to clone a published study and add my own derivative.
The environment needed to operate on it should rebuild automatically, so I do not reconstruct it by hand.

## Extend a study produced by other tools
As a researcher, I want to clone a study whose existing derivatives were made *without* mechababs and add a mechababs-produced derivative alongside them.
mechababs has no prior state file to inherit here, so it starts fresh and must coexist with the existing derivatives without disturbing them.

## Move a run to another cluster
As a user with access to more than one cluster, I want to run the same pipeline configuration on a different cluster by changing only the cluster config.

## Consolidate results across clusters
As a user who has run a campaign on more than one cluster, I want the member studies each cluster worked on to come back together into one superstudy, so that where a study was computed is an operational detail and not a fork in the record.
💬 This may be no more than a plain `git merge` — worth checking before designing anything.

## Add a dataset to a running campaign
As a user, I can add a dataset to a campaign after it has started, and the reconciler picks it up on the next tick.

## Handle a source dataset that changes mid-campaign 💬
As a user, I can handle a source dataset changing after processing has started — new subjects or sessions, or changed data on subjects/sessions already processed.
This is likely handled at the BABS level rather than in mechababs; needs discussion.

## Be able to operate on a crippled filesystem
As a user, I can still collect derivatives with correct provenance on a datalad "crippled filesystem" — one without symlink support, where git-annex runs on an adjusted branch.
