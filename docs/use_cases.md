# mechababs use cases

The user stories mechababs is designed to serve — the requirements the rest of the design answers to.
A mix of what works today and what we are building toward; expected to grow.
When a design decision is unclear, it should be resolvable by asking "which use case does this serve?"

## Sweep many pre-made studies
As a mechababs user, I want to operate on ~1000 pre-made BIDS studies that each contain one raw source dataset.
I want to run MRIQC, fMRIPrep `--anat-only`, and fMRIPrep `--level minimal`, where minimal takes its inputs from the anat run.
I want to prioritize finishing and publishing whole datasets over running the first stage of all thousand, so results land incrementally.

## Work in a single study, no campaign
As a researcher with a single BIDS study, I want to run a BIDS App on it without setting up a campaign.
The study is the thing I operate on; the many-study machinery should stay out of my way.

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
