# mechababs quickstart

This walks one study through one campaign on your cluster, end to end, with a simulated BIDS App so the first run is fast and cheap.
Swap in a real app config once it works.
The words used here are in the [glossary](glossary.md); the ideas behind them are in the [overview](overview.md).

## Before you start

You need `git`, `git-annex`, `datalad`, `uv`, and `apptainer` (or `singularity`) on your PATH, on the login node and the compute nodes.
Nothing else is installed globally; mechababs itself arrives with the campaign.
HPC specifics, including the `git-annex` that is usually missing and where scratch should go, are in [installation.md](installation.md).
Work on fast scratch, never home or `/tmp`, and inside `tmux` or `screen` so a dropped connection does not kill a long run.

## 1. Get a study

mechababs works on a study that already exists: raw data under `sourcedata/`, derivatives under `derivatives/`.
Clone one from OpenNeuroStudies and step into it.

```bash
datalad clone https://github.com/OpenNeuroStudies/study-ds000001
cd study-ds000001
```

Every command from here on runs from this directory.

## 2. Get your configs

A campaign runs your configs: one per BIDS App, and one for your cluster.
Copy the starters out of the mechababs repo and put them somewhere outside the study, such as `~/config/`.

```bash
git clone https://github.com/con/mechababs ~/mechababs-src
mkdir -p ~/config
cp ~/mechababs-src/examples/bids-app-configs/SimBIDS-0.0.3.yaml ~/config/
cp ~/mechababs-src/examples/clusters/dartmouth.yaml ~/config/your-site.yaml
```

Open `your-site.yaml` and set the two things every site differs on: the `script_preamble` that puts the tools on a job's PATH, and `job_compute_space`, the scratch directory jobs clone into.
The SimBIDS config needs no editing.
If your cluster is new to mechababs, the [cluster config and testing tutorial](cluster-config-and-testing-tutorial.md) walks through validating the config before you rely on it.

## 3. Create the campaign

The first command runs mechababs straight from git with `uvx`, so nothing has to be installed first.

```bash
uvx --from git+https://github.com/con/mechababs@main mechababs campaign init demo \
    --apps ~/config/SimBIDS-0.0.3.yaml \
    --cluster ~/config/your-site.yaml \
    --babs https://github.com/PennLINC/babs.git@main \
    --limit 2
```

`demo` is the campaign's label.
`campaign init` copies your configs into `.mechababs/campaigns/demo/`, pins mechababs and babs into a `uv.lock`, builds the campaign's own venv from that lock, and commits the lot into the study.
The lock is the campaign's provenance: it records exactly which code ran.

`--babs` points at babs's `main` because babs has not had a release in a long time, and the released version predates fixes mechababs depends on.
`--limit 2` caps each source dataset at its first two eligible subjects; leave it off for a real run.

## 4. Select the campaign

Each campaign has an `env.sh`.
Sourcing it activates the campaign's venv and names the campaign you are operating on, in one step.

```bash
source .mechababs/campaigns/demo/env.sh
```

Do this in every new shell.
Every mechababs command checks that it is running the selected campaign's venv and that the venv matches the committed lock, and refuses otherwise, so you cannot run the wrong tools by accident.

## 5. Add data

A campaign acts only on the source datasets you select.
This study holds one.

```bash
mechababs add-dataset --sourcedata sourcedata/ds000001
```

This writes one cell per app config into the campaign's statefile: here, one cell.
It does not fetch any data; babs does that inside each job.

## 6. Run it

`iterate` advances every cell by at most one step: scaffold the derivative, then submit its jobs, then merge the results.
Run it until everything is merged.

```bash
mechababs iterate     # scaffold
mechababs iterate     # submit
mechababs status      # watch the jobs
mechababs iterate     # merge, once the jobs are done
```

`status` shows one row per cell with its state and live job counts; `jobs --failed` lists the jobs that ended without results and where their logs are.
A cell whose jobs failed is flagged and left for you; [interventions.md](interventions.md) covers repairing it.
Run `iterate` as often as you like: it re-reads what babs knows every time and only acts on cells that are ready.

## 7. What you get

A new derivative in the study's `derivatives/`, a datalad dataset of its own, plus the study's git history recording the exact commands that made it.
Swap the SimBIDS config for a real one, `examples/bids-app-configs/MRIQC-24.0.2.yaml` say, create a new campaign, and run it the same way.
The [reference](reference.md) has every command and flag.

## Many studies at once

To run one campaign across many studies, create it at a superstudy instead of in a study.
`--superstudy` creates one if it does not exist yet, and `add-dataset --study` clones a member study in before selecting a dataset inside it.

```bash
uvx --from git+https://github.com/con/mechababs@main mechababs campaign init demo \
    --superstudy my-studies \
    --apps ~/config/SimBIDS-0.0.3.yaml --cluster ~/config/your-site.yaml \
    --babs https://github.com/PennLINC/babs.git@main --limit 2
cd my-studies
source .mechababs/campaigns/demo/env.sh
mechababs add-dataset --study https://github.com/OpenNeuroStudies/study-ds000001 \
    --sourcedata sourcedata/ds000001
mechababs iterate
```

`iterate` and `status` then span every member study; `--study study-ds000001` narrows either to one.
