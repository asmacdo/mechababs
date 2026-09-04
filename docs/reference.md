# mechababs — CLI reference

The operational reference: every command, then selection, then the two config files.
The concepts are in [overview.md](overview.md), the words in the [glossary](glossary.md), a first run in the [quickstart](quickstart.md), and the on-disk layout of a study, superstudy, campaign dir and derivative in [output_structure.md](output_structure.md).
Recovering from a failed cell is in [interventions.md](interventions.md).

Two rules hold for every command below.
Commands run from the root of the level the campaign was configured at: the study for a study campaign, the superstudy for a superstudy campaign.
The campaign is selected by sourcing its `env.sh`, which activates its venv and exports `MECHABABS_CAMPAIGN=<label>`; there is no `--campaign` flag and no default when only one exists.

```bash
source .mechababs/campaigns/<label>/env.sh
```

Before acting, a command checks that it is running the selected campaign's own venv and that the venv matches the campaign's committed `uv.lock` (`uv sync --check`).
It refuses on either mismatch and names `campaign update-env` as the fix.
The two exceptions are `campaign init`, which runs before the venv exists, and `campaign update-env`, whose job is to run when the check fails.

## `campaign init`

Create a campaign inside an existing study, or at a superstudy.
This is the one command that runs before the campaign environment exists, so it runs ephemerally with `uvx`; nothing is installed on `PATH`.

```bash
uvx --from git+https://github.com/con/mechababs@<ref> mechababs campaign init <label> \
    [-d PATH | --superstudy NAME] \
    --apps PATH|URL[,PATH|URL…] [--apps …] \
    --cluster PATH|URL \
    [--limit N] [--babs URL@REF] [--mechababs URL@REF]
```

It copies the named app and cluster configs into `.mechababs/campaigns/<label>/`, pins mechababs and babs into a `pyproject.toml` plus `uv.lock`, builds the campaign's venv from that lock, writes `env.sh`, and commits the result.
Which source datasets the campaign acts on is a separate step, `add-dataset`.

- `<label>` is the campaign's identity: its directory name, and what `MECHABABS_CAMPAIGN` selects.
- `-d PATH` names the study to create the campaign in, the way datalad's `-d` does (default: the current directory).
  `--superstudy NAME` instead creates the campaign at a superstudy of that name, creating the superstudy if it is not there yet and adopting it if it is.
  The two are mutually exclusive.
- `--apps` takes app configs by path or URL, comma-separated and repeatable; their order is the bundle's order.
  `--cluster` takes one cluster config by path or URL.
  Configs are yours; `examples/` in the mechababs repo holds starters to copy (see [Configuration](#configuration)).
- `--limit N` caps each source dataset's inclusion to the first N eligible subjects (default: all). It is recorded in the campaign's `campaign.yaml`.
- `--babs URL@REF` pins babs to a git checkout instead of the default, the latest babs release, frozen to an exact version by the lock.
  `URL` is anything git clones, a local checkout included, which is how a babs branch gets run through a campaign.
  The released babs cannot resolve an image out of a ReproNim/containers dataset (`PennLINC/babs#399` is merged but in no release), so a campaign whose app configs point at one pins babs by ref, for example `--babs https://github.com/PennLINC/babs.git@main`.
- `--mechababs URL@REF` pins a different mechababs (default: the one running the command, pinned by its resolved commit).

Init never clobbers, and it does not scaffold input studies.
What it does is decided by what is at the target: nothing there, and `--superstudy` given, creates the superstudy as a datalad dataset and the campaign inside it (a study is never created this way); an existing datalad dataset gains a `.mechababs/` at its root, with any campaigns already there untouched; a non-empty directory that is not a dataset is refused; a campaign with this label already present is refused, by name.
Init commits at the study or superstudy root, so it holds the level's single-writer lock like every other command that does, and cannot land while an iterate there is mid-save.

At a superstudy, `campaign init` touches only the superstudy's own campaign dir.
A member study gets its copy of the campaign (configs, `pyproject.toml`, `uv.lock`, an empty statefile) when `add-dataset` first selects a source dataset in it; the venv and `env.sh` exist only at the superstudy.

## `add-dataset`

Select a source dataset that is already in the study into the campaign.

```bash
mechababs add-dataset --sourcedata PATH [--study PATH|URL]
```

`--sourcedata` is a path inside the study, such as `sourcedata/ds000001`; it is refused if it is outside the study, absent, or not a directory.
`add-dataset` reads the study's per-subject metadata to fill the cell's identity columns, then writes one row per app in the campaign's bundle into the study's statefile, each cell empty and unscaffolded.
It does not install data, and it does not generate the subject inclusion; that happens at scaffold.

At a superstudy, `--study` is required and names the member study holding the source dataset, as a path relative to the superstudy or as a URL to clone one in.
`--sourcedata` is then relative to that member.
Cloning a member in is the one case where a selection verb brings something into the tree; source data itself is never fetched.
The superstudy records the member and source dataset in its study catalog, and writes the member's campaign copy if this is the member's first selection.
For a study campaign `--study` is refused, since there is no member to name.

Two refusals to know about.
A dataset that already has any cell in the statefile is refused whole: nothing is rewritten and nothing duplicated, and the app bundle is fixed at `campaign init`, so more apps means a new campaign.
An app whose `depends_on` names a producer that is not in the statefile is refused, so the producer's config must be in the bundle.

## `iterate`

Advance the campaign's cells by at most one transition each: one pass of the reconciler. Each cell that advances is a tick.

```bash
mechababs iterate [--batch N] [--app STEM] [--study MEMBER] [--dry-run]
```

Each cell is routed on its statefile columns:

| `babs` | `merged` | Cell state | What `iterate` does |
|---|---|---|---|
| empty | empty | not started | **scaffold**: generate the inclusion, compose the babs config, `babs init`, pin the inclusion, record the derivative path in `babs` |
| set | empty | active | read `babs status` and decide from the counts: not all submitted, **submit**; still in flight, wait; all ended with failures, **flag**, do not merge; all done, **merge** |
| set | set | merged | skip |

A cell whose `depends_on` producer is not yet merged is noted as waiting and passed over, not blocked on; the next iterate re-checks.
A cell whose jobs failed is flagged rather than merged, and the other cells keep going.
Nothing is remembered between runs: every iterate re-reads the statefile and the live babs state, so you run `iterate` again and again until every cell is merged.

- `--batch N` stops after N ticks (default: all). A cell that is passed over because it is already merged, waiting, still running, or failed is not a tick and does not count against it.
- `--app STEM` narrows to one app config's cells, by its filename stem (`MRIQC-24.0.2`).
- `--study MEMBER`, at a superstudy, advances only that member study. Composable with `--app`.
- `--dry-run` routes every cell for real and prints the transitions it would dispatch, without dispatching them.

At a superstudy the iterate runs over every installed member in study catalog order, and `--batch` is the whole iterate's budget rather than each member's, so `--batch 5` advances the five cells that come first across the superstudy.
A member study that has been uninstalled is skipped, never reinstalled.

Scaffold and merge are recorded with `datalad run` at the study, so the study's git history carries the command that produced each derivative; submit changes nothing tracked and is not recorded.
At a superstudy each scaffold or merge is also committed at the superstudy, as the member's moved gitlink, one commit per tick and before the next cell is attempted.
An iterate takes the level's single-writer lock, and refuses to start on a dirty tree.

## `status`

One row per cell of the selected campaign, read-only.

```bash
mechababs status [--study MEMBER] [--app STEM]
```

Columns: `source_dataset`, `app`, `level`, `subjects`, `sessions`, `state`, `jobs`.
`state` is `not started`, `waiting` (its producer is not merged yet), `active`, `merged`, or `FAILED`, the last for an active cell whose live counts say jobs ended without results; `jobs` carries the live `babs status` counts for active cells.
The table goes to stdout and the summary line to stderr, so `status | grep FAILED` sees rows and only rows.
`status` takes no lock, so it can be run while an iterate is in progress.

At a superstudy the rows span every member study, computed from their statefiles at the moment you look, and gain two leading columns: `study`, and `installed`.
`installed` is `yes` only when the member and that cell's derivative are both on disk, so a finished cell whose output has since been offloaded reads `merged` plus `no`.
A member with no working tree on disk cannot be read; its cells show the study catalog's lifecycle in `state`, or `unknown`.

- `--study MEMBER` narrows to one member. It is matched against the study catalog, so a directory that was never selected in is an error rather than an empty table.
- `--app STEM` narrows to one app config. It is checked against the campaign's declared apps, so a typo is refused even when nothing is installed to compare against.

## `jobs`

One row per job babs is tracking, read-only: the drill-down under `status`.

```bash
mechababs jobs [--study MEMBER] [--app STEM] [--failed] [--no-refresh]
```

Columns: `source_dataset`, `app`, `sub_id`, `ses_id`, `job_id`, `state`, `time_used`, `time_limit`, `failed`, `logs`, with a leading `study` at a superstudy.
`job_id` is `<job>_<task>`, SLURM's own array addressing, so it pastes straight into `sacct -j`.
`logs` is the cell's log directory, resolved from where you are standing.
A cell with nothing submitted has no jobs yet and is left out.

`jobs` reads each derivative's `code/job_status.csv`, which babs recomputes from the scheduler, and refreshes it first.
Reading the file as it stands can be wrong rather than merely stale: babs rewrites a resubmitted row's job id without clearing the previous attempt's failure mark, so a running job can show as failed.
`--study` and `--app` narrow before the refresh, so scoping keeps it fast.

- `--failed` shows only jobs babs marks failed (ended without results).
- `--no-refresh` reads `job_status.csv` as it stands. Faster, and an explicit choice; the header then says the table was not refreshed.

## `retire-derivative`

Take a derivative out of its study and reset the cell that made it, so the next `iterate` re-scaffolds it.

```bash
mechababs retire-derivative PATH (--path DEST | --remove)
```

`babs init` refuses an existing path, so a cell that has to be redone (a resource change, a tool bug, a config fix) cannot be re-scaffolded until its derivative leaves.
`PATH` is the derivative, campaign-relative or absolute: `derivatives/<name>` at a study, `<member>/derivatives/<name>` at a superstudy.
Where it goes is a required choice, because keeping the evidence and throwing it away are different decisions.

- `--path DEST` archives it at `DEST/<study>-<derivative>-attempt-<N>`, first free N, keeping its logs, git history and run records.
  `DEST` must be outside the study, and outside the whole superstudy when the campaign is operated at one, checked on the resolved path; a study is a published object, and a retired attempt inside it would travel with it.
- `--remove` deletes the derivative outright, for a cell whose evidence is worth nothing.

The cell is reset in the same transition, so there is no window where the derivative is gone but the reconciler still routes it as in-progress.
Only the derived columns are blanked; the cell's identity stays, and so does its membership at a superstudy.
A derivative no cell claims is refused before anything moves.

An archived derivative is evidence, not a resumable babs project.
babs bakes absolute RIA paths in at init, so after the move its `input` and `output` siblings still name the old location: babs commands will not operate on it, and neither will `datalad get` or `push` through those siblings.
Read its logs, history and content; retire a cell you mean to redo from scratch, not one you mean to continue.

## `campaign update-env`

Converge the campaign's environment on its declaration.

```bash
mechababs campaign update-env [--upgrade PKG]… [--study MEMBER]
```

It re-resolves `pyproject.toml` into `uv.lock`, installs exactly that into the campaign venv, and commits both if either moved.
What it does follows from the declaration.
Untouched, the lock does not move and the venv is rebuilt from it: a fresh clone, a wiped site, a historical checkout during reproduction.
Edited, the change re-resolves and installs: the deliberate mid-campaign bump.
To bump, edit `.mechababs/campaigns/<label>/pyproject.toml` (the pins are `rev` lines under `[tool.uv.sources]`) and run this.

A bump reaches only cells that have not been scaffolded yet; a scaffolded cell keeps the job scripts babs baked at init.
To move a scaffolded cell onto the new environment, retire it and let `iterate` re-scaffold.
Completed cells keep the lock that produced them, and the campaign becomes deliberately heterogeneous, recorded in the lock's git history.

- `--upgrade PKG` re-resolves PKG to the newest thing its declaration allows without editing the declaration: the case with nothing to hand-edit, a pin tracking a branch whose tip moved. Repeatable; touches only the lock.
- `--study MEMBER`, at a superstudy, also copies the resulting lock into that member's campaign dir as its own commit.
  A member whose lock copy is behind the superstudy's is refused by the inner verbs until this is done, so moving a member's remaining work onto the new environment is an explicit act.
  The lock only; the member's configs are never touched.

It is committed as a plain save rather than a `datalad run`: `uv lock` resolves against the live world, so recording it as re-executable would be a false promise.
The lock file is the reproducible artifact.

## `test-cluster`

Validate a cluster config end to end, in a throwaway study.

```bash
uvx --from 'git+https://github.com/con/mechababs@<ref>#egg=mechababs[test]' \
    mechababs test-cluster --cluster PATH --scratch-path DIR \
    [--babs URL@REF] [--mechababs URL@REF] [-- PYTEST_ARGS]
```

It runs the packaged e2e scenario against your cluster config: `campaign init`, `add-dataset`, then `iterate` through scaffold, submit and merge, asserting a real derivative landed.
A stronger check than `babs check-setup`, because it proves the config produces output on this scheduler.
The scenario builds its own fixture study under `--scratch-path` and works there; real studies are never touched.

- `--cluster PATH` is the cluster config to validate, by path.
- `--scratch-path DIR` is where the fixture studies, the container dataset they resolve as their sibling, and the caches go. Put it on fast cluster scratch, never home or `/tmp`.
- `--mechababs URL@REF` overrides the mechababs the fixture campaign pins. Unset, it pins whichever mechababs is running the command, so what is validated is the code you invoked, by the same path a user's own `campaign init` takes.
- `--babs URL@REF` overrides the babs. Unset, the fixture campaign gets what a user's campaign would get: the latest release, frozen by the campaign's lock. babs is not a mechababs dependency, so it cannot mirror the caller.
- Arguments after a literal `--` pass through to pytest, for example `-- -k test_spine`.

The suite runs with the invoking interpreter's pytest, which is why the invocation installs the `test` extra; it cannot come from the fixture campaign's venv, which does not exist when pytest starts.
The [cluster config and testing tutorial](cluster-config-and-testing-tutorial.md) is the full walk-through.

## Selection and inclusion

Which subjects a cell's jobs run over is decided at scaffold, from the app config's `selection:` rule and the study's per-subject metadata TSV (per subject or session: `datatypes`, `t1w_num`, `bold_num`), as OpenNeuroStudies provides.

```yaml
mechababs:
  selection:
    require_datatypes: [anat, func]   # every one present
    require_positive: [t1w_num, bold_num]   # every one > 0
```

The rule names TSV columns directly, so a new app's needs are config, not code.
The eligible list is sorted, truncated to the campaign's `--limit` (a reproducible first N), formatted to the cell's `processing_level` (`sub_id`, or `sub_id` and `ses_id`), and handed to `babs init --list-sub-file`, which defines the job universe.
babs inner-joins it with the subjects present in the data and records the result as `processing_inclusion.csv` inside the derivative.

mechababs also pins the requested list beside the statefile, at `.mechababs/campaigns/<label>/inclusions/<sourcedata>_<app>.csv`, where `<sourcedata>` is the study-relative path with `/` mapped to `-` and `<app>` is the config's filename stem.
The pin is written inside the cell's `datalad run`, so it travels with the study; its diff against babs's `processing_inclusion.csv` is what catches a selected subject the data does not have.

If a file already exists at the pin path when the cell scaffolds, it is used as-is and selection is skipped.
So a one-row file there before the first iterate runs a one-subject smoke test of the whole cell:

```bash
mkdir -p .mechababs/campaigns/<label>/inclusions
printf "sub_id\nsub-01\n" > .mechababs/campaigns/<label>/inclusions/sourcedata-ds000001_MRIQC-24.0.2.csv
mechababs iterate --batch 1
```

## Configuration

A campaign holds two kinds of config, and they are separate because they change for different reasons.
An **app config** is the BIDS App's flags, container and zip layout, reviewed as science; a **cluster config** is the site's resources and job preamble, often private.
Never bake a cluster detail into an app config or the reverse: the same app runs on another site by swapping one file.
For each cell, mechababs composes app config × cluster config × source dataset into the single YAML `babs init --container-config` takes, in a temporary file; babs keeps the resolved copy that ran at the derivative's `.babs/babs_init_config.yaml`.

Both are yours, given to `campaign init` by path or URL and copied into the campaign dir (`bids-app-configs/`, `clusters/`).
Edit them there and commit; a change reaches only cells not yet scaffolded, since babs bakes the job scripts at init.
`examples/bids-app-configs/` and `examples/clusters/` in the mechababs repo are starters to copy, with the site paths to edit marked `SITE`.

### App configs

The filename stem is the app's identity: what `--app` takes, what `depends_on` names, and the first part of the derivative's name (`MRIQC-24.0.2+ds000001+c1`: app, source dataset, campaign label).
The `mechababs:` block at the top is read by mechababs and stripped before the config reaches babs; everything else is babs container-config, passed through.

```yaml
mechababs:
  selection: {…}                  # the eligibility rule, above
  depends_on: fMRIPrep-25.2.5+anat   # optional: the producer's stem; ordering only
  container:
    source: https://github.com/ReproNim/containers.git   # URL, or a path (absolute, or relative to the study root)
    name: bids-fmriprep
```

- `container.source` is a ReproNim/containers dataset, handed to `babs init --container-ds` as-is; babs installs it into every derivative it inits, so a path to a local clone saves a fresh clone per cell.
  `name` is the image's datalad-containers name.
- `depends_on` delays this app's cell until the named producer's cell for the same source dataset is merged.
  It wires nothing.
  A stage that consumes the producer's output also declares it under babs's `input_datasets`, keyed by the producer's stem and with no `origin_url`: the producer's output store does not exist until it has merged, so scaffold injects it per run.
  A QC gate names its producer once, in `depends_on` only.
- `analysis_path`, `input_ria_path` and `output_ria_path` place the babs RIA stores under `.babs/` so the derivative root is the derivative.
  They must match across every app config in a campaign.
- `zip_foldernames` is keyed by this config's stem, which names the output folder babs zips and is the handle a downstream stage's `input_datasets` chains onto.
- `script_preamble` and the other job-level keys belong to the cluster config; resource requests (`cluster_resources`) are parked in the app configs for now (con/mechababs#3, #97).

### Cluster configs

```yaml
script_preamble: |
  source "{{MECHABABS_VENV}}/bin/activate"
  export JOB_TMP="/scratch/${USER}/sjob-tmp/${SLURM_ARRAY_JOB_ID}_${SLURM_ARRAY_TASK_ID}"
  mkdir -p "${JOB_TMP}"
  trap 'rm -rf "${JOB_TMP}"' EXIT
job_compute_space: "/scratch/${USER}"
# env_constraints:      # optional, verbatim PEP 508 caps for a site whose glibc is old
#   - scipy<=1.15.3
```

- `script_preamble` runs at the top of every job. It must put the campaign venv, `git` (2.25 or newer) and `git-annex` on the job's `PATH`; `{{MECHABABS_VENV}}` is replaced with the venv's real path when the babs config is composed, so no absolute path is committed.
  `$JOB_TMP` is the per-job scratch the app configs bind to the container's `/tmp`.
- `job_compute_space` is where babs clones the derivative for each job. Fast scratch, and outside any quota a running sweep could fill.
- `env_constraints` caps package versions the campaign environment may install, as uv `constraint-dependencies`; a modern cluster declares none.
  When `campaign init` still hits a package with no installable wheel, it fails naming that package and pointing here.

The [cluster config and testing tutorial](cluster-config-and-testing-tutorial.md) walks through adapting a starter to your site and validating it with `test-cluster`.
