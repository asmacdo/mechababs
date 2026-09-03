# mechababs

Run BIDS Apps over many studies on an HPC cluster, with [BABS](https://github.com/PennLINC/babs) running the jobs.

mechababs works on a BIDS study, adding derivatives to it, and records how each one was made inside the study itself: the exact `mechababs` and `babs` that ran, pinned in a lock, and every orchestration step as a `datalad run` record.
A **campaign** is one such run's recipe, your app and cluster configs plus that locked environment, and `mechababs iterate` moves the study toward what you declared, one pass at a time.
A **superstudy** groups many studies so one campaign sweeps them all.

## Quickstart

```bash
datalad clone https://github.com/OpenNeuroStudies/study-ds000001 && cd study-ds000001
uvx --from git+https://github.com/con/mechababs@main mechababs campaign init demo \
    --apps ~/config/SimBIDS-0.0.3.yaml --cluster ~/config/your-site.yaml \
    --babs https://github.com/PennLINC/babs.git@main --limit 2
source .mechababs/campaigns/demo/env.sh
mechababs add-dataset --sourcedata sourcedata/ds000001
mechababs iterate      # repeat until every cell is merged
```

The [quickstart](docs/quickstart.md) walks through it, configs included.
New cluster? Put the prerequisites in place ([installation.md](docs/installation.md)), then validate your cluster config with `mechababs test-cluster` ([tutorial](docs/cluster-config-and-testing-tutorial.md)).

## Docs

- [docs/quickstart.md](docs/quickstart.md) — one study through one campaign, end to end.
- [docs/overview.md](docs/overview.md) — the concepts: the study as the unit, the campaign as a recipe recorded in it, the reconciler.
- [docs/glossary.md](docs/glossary.md) — the words, defined once.
- [docs/reference.md](docs/reference.md) — every command and flag, selection, and the two config files.
- [docs/interventions.md](docs/interventions.md) — when a cell fails: find it, repair it in place, or redo it.
- [docs/installation.md](docs/installation.md) — HPC prerequisites: PATH tools, scratch, the container dataset.
- [docs/cluster-config-and-testing-tutorial.md](docs/cluster-config-and-testing-tutorial.md) — add your cluster and validate it.
- [docs/output_structure.md](docs/output_structure.md) — the on-disk shape of a study, a superstudy, a campaign, and a derivative.
- [docs/spec.md](docs/spec.md) — the design decisions of record, and [docs/use_cases.md](docs/use_cases.md), the user stories they answer to.
- [CONTRIBUTORS.md](CONTRIBUTORS.md) — developing and testing mechababs itself.
- [CLAUDE.md](CLAUDE.md) — contributor conventions and issue tracking.
- Open work and milestones live in the GitHub tracker.

## Upstream

- [OpenNeuroStudies](https://github.com/OpenNeuroStudies/OpenNeuroStudies) — the superstudy mechababs feeds
- [OpenNeuroDerivatives](https://github.com/OpenNeuroDerivatives/OpenNeuroDerivatives) — derivative mirrors + the fmriprep opinions repo
- [BABS](https://github.com/PennLINC/babs) — the execution engine
- [ReproNim/containers](https://github.com/ReproNim/containers) — container datasets
- [FAIRly Big processing workflow](https://github.com/psychoinformatics-de/fairly-big-processing-workflow) — the pattern BABS implements
- [STAMPED principles](https://github.com/stamped-principles/stamped-paper) — the guiding principles
