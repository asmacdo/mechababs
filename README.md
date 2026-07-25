# mechababs

Automation glue for running BIDS apps across many OpenNeuro datasets on HPC
clusters using [BABS](https://github.com/PennLINC/babs).

mechababs is an end-to-end harness for running BABS across clusters and many datasets.
It runs **vanilla** BABS by default (`PennLINC/babs` main, or a PR branch under test), and can use a babs fork when one is needed.
The unit of work is a **campaign**: a self-contained datalad dataset that holds its inputs, its outputs, its config, its state ledger, and the exact `babs` + `mechababs` code that produced everything.

Every run composes three axes — **a dataset** × **a pipeline** (`pipelines/*.yaml`) × **a cluster** (`clusters/*.yaml`) — and `mechababs iterate` reconciles the campaign toward the state you declared, one tick at a time.

## Quickstart

```bash
# 1. Build a campaign (needs only git + uv on PATH; run once per campaign).
curl -sSL https://raw.githubusercontent.com/con/mechababs/main/bootstrap.sh \
  | bash -s -- my-campaign

cd my-campaign
source .venv/bin/activate

# 2. Bind an ordered pipeline-set to a cluster (configure copies the named configs into the campaign).
mechababs configure --pipelines code/mechababs/examples/pipelines/MRIQC-24.0.2.yaml --cluster code/mechababs/examples/clusters/dartmouth.yaml [--limit N]

# 3. Register datasets by URL.
mechababs add-dataset https://github.com/OpenNeuroDatasets/ds005896

# 4. Advance the campaign one reconciler tick at a time until it's done.
mechababs iterate
```

New to a cluster? Get the prerequisites in place ([installation.md](docs/installation.md)), then validate your HPC config by running the e2e suite on it — see the [cluster config & testing tutorial](docs/cluster-config-and-testing-tutorial.md).

## Docs

- [docs/overview.md](docs/overview.md) — the concepts: the three axes, the campaign as a self-contained provenance object, and the reconciler tick.
- [docs/installation.md](docs/installation.md) — HPC prerequisites: PATH tools, scratch, the container shim, and the e2e driver venv.
- [docs/reference.md](docs/reference.md) — CLI reference, selection & inclusion, and the config files.
- [docs/interventions.md](docs/interventions.md) — recovering from failures and changing a running campaign, provenance-safely.
- [docs/cluster-config-and-testing-tutorial.md](docs/cluster-config-and-testing-tutorial.md) — add your cluster and validate it by running the e2e suite on it.
- [docs/output_structure.md](docs/output_structure.md) — the target on-disk shape of a campaign and everything it produces.
- [CLAUDE.md](CLAUDE.md) — project conventions, the pipeline, terminology, and milestones.
- [CONTRIBUTORS.md](CONTRIBUTORS.md) — developing and testing mechababs itself.
- Open work + milestones live in the GitHub tracker.

## Upstream

- [OpenNeuroStudies](https://github.com/OpenNeuroStudies/OpenNeuroStudies) — the superdataset mechababs feeds
- [OpenNeuroDerivatives](https://github.com/OpenNeuroDerivatives/OpenNeuroDerivatives) — derivative mirrors + the fmriprep opinions repo
- [BABS](https://github.com/PennLINC/babs) — the execution engine
- [ReproNim/containers](https://github.com/ReproNim/containers) — container datasets
- [FAIRly Big processing workflow](https://github.com/psychoinformatics-de/fairly-big-processing-workflow) — the pattern BABS implements
- [STAMPED principles](https://github.com/myyoda/principles-paper) — the guiding principles
