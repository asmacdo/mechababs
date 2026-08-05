# mechababs use cases

The user stories mechababs is designed to serve — the requirements the rest of the design answers to.
A mix of what works today and what we are building toward; expected to grow.
When a design decision is unclear, it should be resolvable by asking "which use case does this serve?"

The whole document is open for feedback; 💬 marks the points that specifically need it — open questions we want others to weigh in on.

## Sweep many pre-made studies
As a mechababs user, I want to operate on ~1000 pre-made BIDS studies that each contain one raw source dataset.
I want to run MRIQC, fMRIPrep `--anat-only`, and fMRIPrep `--level minimal`, where minimal takes its inputs from the anat run.
I want to prioritize finishing and publishing whole datasets over running the first stage of all thousand, so results land incrementally.

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

## Run a study to completion under a finite budget
As a user with limited disk and inodes, I can sweep more studies than fit at once, because finishing and releasing a study frees the space the next one needs.
💬 Whether a single study's own peak footprint fits is a separate problem, not covered by this.

## Work in a single study, no super-study
As a researcher with a single BIDS study, I want to run a BIDS App on it without setting up a super-study.
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

## Add a dataset to a running campaign
As a user, I can add a dataset to a campaign after it has started, and the reconciler picks it up on the next tick.

## Handle a source dataset that changes mid-campaign 💬
As a user, I can handle a source dataset changing after processing has started — new subjects or sessions, or changed data on subjects/sessions already processed.
This is likely handled at the BABS level rather than in mechababs; needs discussion.

## Be able to operate on a crippled filesystem
As a user, I can still collect derivatives with correct provenance on a datalad "crippled filesystem" — one without symlink support, where git-annex runs on an adjusted branch.
