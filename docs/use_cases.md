# mechababs use cases

The user stories mechababs is designed to serve — the requirements the rest of the design answers to.
A mix of what works today and what we are building toward; expected to grow.
When a design decision is unclear, it should be resolvable by asking "which use case does this serve?"

## Terminology
The words below are defined in the [glossary](glossary.md), and used consistently throughout this document and [output_structure.md](output_structure.md).

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
As a user, I can direct mechababs at a specific study and have it advance only that one, so I can concentrate limited cluster resources on finishing a study rather than spreading progress across the whole set (see [Run a study to completion under a finite budget](#run-a-study-to-completion-under-a-finite-budget)).
The selector is an explicit study argument: the working directory gives the operating *level* (a campaign is operated from where it was configured), and the argument narrows within it.
Scoping by working directory alone was considered and rejected — the superstudy still takes writes when a study finishes, so the working directory cannot be the selector.

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
As a user with limited cluster resources — disk and inodes, but also job slots, CPU, and RAM — I can sweep more studies than fit at once, because finishing and releasing a study frees what the next one needs.
Storage is the binding one: getting a finished study *off* the cluster as soon as it is done is how space is reclaimed, so the workflow favors finishing whole studies over advancing every study's first stage.
This is *why* order ([Choose what gets worked next](#choose-what-gets-worked-next)) and a per-study [selector](#act-on-one-study-within-a-superstudy) matter — they are how the user concentrates finite resources on completing-and-offloading.
Whether a single study's own peak footprint fits is a separate problem, not covered by this.

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

## Reproduce the orchestration from the study alone
As a collaborator with only a cloned study, I can rebuild the environment it records and re-run the recorded orchestration commands to reproduce how its derivatives were made.
Everything required — the campaign's config and pinned environment — is committed inside the study itself; no superstudy or outside record is needed.

## Extend a study produced by other tools
As a researcher, I want to clone a study whose existing derivatives were made *without* mechababs and add a mechababs-produced derivative alongside them.
mechababs has no prior state file to inherit here, so it starts fresh and must coexist with the existing derivatives without disturbing them.

## Move a run to another cluster
As a user with access to more than one cluster, I want to run the same pipeline configuration on a different cluster by changing only the cluster config.

## Consolidate results across clusters
As a user who has run a campaign on more than one cluster, I want the member studies each cluster worked on to come back together into one superstudy, so that where a study was computed is an operational detail and not a fork in the record.
What comes together is the **results** — derivative data, portable in git/annex — not running machinery; it may amount to a `git merge`, with conflicts hand-resolved.

## Add a dataset to a running campaign
As a user, I can add a dataset to a campaign after it has started, and the reconciler picks it up on the next iterate.
This covers each shape of adding: a source dataset already in my study, another source dataset of a study the campaign already works on, and bringing a whole existing study in to point at a source dataset inside it.

## Start a campaign in a superstudy that has run campaigns before
As a user with a superstudy I have already swept once — last year's tool versions, its own config, its own record of what was produced — I can start a *new* campaign in it without disturbing any of that.
The new campaign is a distinct label with its own pinned environment and config bundle, so the two coexist: the old campaign's record stays exactly as it was, and nothing about the earlier sweep is rewritten to accommodate the new one.
The command that does this is the same one that creates a superstudy from nothing, so adopting what already exists is the ordinary path rather than a migration mode.
Member studies that were part of the earlier sweep are not automatically part of the new one — a campaign's membership is chosen by selecting into it, so I decide which studies this sweep covers.

## Adopt a new tool release mid-sweep
As a user running a large sweep, when a bug is found and fixed in a new release, I can bump the campaign's pinned version without discarding completed work: finished derivatives stay as they are, new work uses the new version, and I can selectively redo the cells the bug affected.

## Handle a source dataset gaining subjects or sessions
As a user, when new subjects or sessions are added to a source dataset after processing has started, the reconciler picks them up on a later iterate and processes them alongside the rest.
This is the additive case: no already-produced derivative is invalidated, so it is the same shape as adding a dataset to a running campaign, one grain finer.

## Handle a source dataset's data changing after processing
As a user, I can handle data changing on subjects or sessions that have already been processed — the harder case, because derivatives already produced from that data may now be stale.
This likely belongs at the BABS level rather than in mechababs, and overlaps what Yarik is exploring in OpenNeuroStudies (representing such state changes uniformly across a submodule hierarchy); it is open, to be discussed.

## Be able to operate on a crippled filesystem
As a user, I can still collect derivatives with correct provenance on a datalad "crippled filesystem" — one without symlink support, where git-annex runs on an adjusted branch.

## Out of scope

Things mechababs deliberately does not do.
These earn a place beside the use cases because they carry the same weight: a design that requires one of them is answering the wrong question.
Each is paired with what mechababs must do instead, since "not our job" is only half a requirement.

### mechababs never pushes to a remote
As a user, I decide when and where results leave the machine they were computed on.
Publishing a derivative, a study, or a superstudy is my act, with my credentials, on my timing; mechababs produces the objects and reports that they are finished.
What mechababs must do instead is be unbothered by it — a study I have pushed and removed is not a state it tries to repair (see [Release a finished study](#release-a-finished-study)).

The RIA stores babs pushes to during a run are not an exception to this: they are machinery internal to the derivative, alongside it on the same filesystem, not a publishing destination.

### mechababs never removes or drops data
As a user, I decide what content is dropped and what datasets are uninstalled — those are destructive and, where no other copy exists, irreversible.
mechababs does not reclaim space on my behalf, however sure it is that a study is finished.

What mechababs must do instead is *tolerate* the result: content I dropped, or a study I uninstalled, is a normal state and not a failure.
It should still be able to report what that study's state was, and get back whatever a step actually needs rather than assuming content is present.
This is what makes [Run a study to completion under a finite budget](#run-a-study-to-completion-under-a-finite-budget) work — I free the space, mechababs keeps going.
