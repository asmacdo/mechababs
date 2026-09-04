# mechababs — overview

mechababs runs [BIDS Apps](https://bids-apps.neuroimaging.io/) over many studies on an HPC cluster, using [BABS](https://github.com/PennLINC/babs) to run the jobs.
The unit it works on is a [BIDS study](https://bids-specification.readthedocs.io/en/stable/common-principles.html#study-dataset): raw data grouped with the derivatives made from it.
mechababs adds derivatives to a study that already exists; it never authors one.

The words used here are defined in the [glossary](glossary.md).

## The objects

A **study** is a datalad dataset holding one or more source datasets under `sourcedata/` and their derivatives under `derivatives/`.
A **derivative** is what one BIDS App produced from one source dataset, itself a datalad dataset, created in its final home inside the study and never moved afterwards.
A **superstudy** is a study whose members are studies: an optional layer for running many at once, holding no raw data of its own.
[OpenNeuroStudies](https://github.com/OpenNeuroStudies/OpenNeuroStudies) is one.

```
superstudy/                       # optional
  study-ds000001/                 # a member study
    sourcedata/ds000001/          #   a source dataset
    derivatives/
      MRIQC-24.0.2+ds000001+c1/   #   a derivative mechababs added, by campaign c1
      fMRIPrep-25.2.5+anat+ds000001+c1/
    .mechababs/campaigns/<label>/ #   the campaign's record in this study
  study-ds000002/
  .mechababs/campaigns/<label>/   # the campaign's record at the superstudy
```

The full layout, and the reasoning behind it, is in [output_structure.md](output_structure.md).

## The campaign

A **campaign** is one processing run's recipe: a bundle of app configs, a cluster config, and a locked environment that pins mechababs and babs together.
It is not a dataset.
It is a directory inside the study (or superstudy) at `.mechababs/campaigns/<label>/`, committed there, so the study carries the record of every campaign that touched it and stays reproducible on its own.
A study accumulates campaigns over time: a set of derivatives now, another a year later with newer tools, each under its own label.

The configs are yours.
An app config holds one BIDS App's flags and container; a cluster config holds the site's resources and job preamble.
For each cell mechababs composes app × cluster × source dataset into the one config babs takes, so the same app runs on another site by swapping one file.

The environment is a `uv.lock`, and the lock is the pin.
`campaign init` builds the campaign's venv from it, and every later command refuses to run unless it is running that venv and the venv matches the lock.
A version bump edits and commits the lock, so the lock's evolution is its git history, and a cell can always be traced to the exact code that produced it.

A campaign is operated only from the level it was configured at: the study for a study campaign, the superstudy for a superstudy campaign.
Selecting it is sourcing its `env.sh`.

## Cells and the reconciler

A **cell** is one source dataset × one app config, and it produces one derivative.
The campaign's statefile has one row per cell, with two derived columns: `babs`, set once the derivative is scaffolded, and `merged`, set once the results are merged in.
There is no status enum; a cell's state is read off those two columns.

`iterate` is one pass of a reconciler, and each cell it advances by one transition is a **tick**.
It advances every cell by at most one transition: an unscaffolded cell is scaffolded (`babs init`); a scaffolded cell is asked what babs says about its jobs, and is submitted, waited on, merged, or flagged; a merged cell is skipped.
You run it again and again until everything is merged.

An app config may declare `depends_on` another: its cell waits until the producer's cell for the same source dataset is merged.
That is how a staged pipeline is expressed, one app config per stage, and it is ordering only; how a stage consumes the producer's output is babs's input wiring.

You declare intent (`campaign init`, `add-dataset`) and `iterate` moves reality toward it.
`iterate` is **level-triggered**: it re-reads ground truth every time, rather than reacting to events as they happen.
A missed event in an event-driven system is permanent drift; a level-triggered loop simply picks up where things stand, which is what lets a long, interrupted campaign converge.

## Provenance

Every change-making transition is recorded with `datalad run` at the study, so the study's git history holds the exact command that scaffolded or merged each derivative.
The jobs themselves are recorded by babs inside the derivative, as `datalad run` records of each `singularity run`.
Two record homes: orchestration in the study, compute in the derivative.

Together with the committed lock, that makes a study a self-contained, re-executable research object: clone it, rebuild the environment from the lock, and rerun a recorded command.
That is the [STAMPED](https://github.com/stamped-principles/stamped-paper) payoff mechababs is built for, and the reason it sits on top of babs rather than beside it.

## Failures stop

Only durable facts are stored: scaffolded, merged.
Everything volatile, including job status, `waiting`, and `FAILED`, is re-read from babs each iterate, so nothing goes stale and a flag clears itself once the cause is fixed.
When jobs fail for a reason a human has to decide about, the cell is flagged and left alone; the other cells keep going.
Recovery is a human act, and the campaign records it rather than smoothing it away.
See [interventions.md](interventions.md).

## One tool, two modes

A throwaway study with one subject and a babs branch under test, running several bids-apps over one study, and a production sweep over OpenNeuroStudies with released code, are the same tool with different configs and content.
There is no dev-only branch, field, or code path, so a dev run exercises exactly what production will.
