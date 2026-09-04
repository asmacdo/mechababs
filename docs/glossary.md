# mechababs glossary

The words the mechababs docs use, defined once.
Ordered from the outside in: the objects on disk first, then the campaign that acts on them, then the reconciler's own vocabulary.

## Objects on disk

- **source dataset**: a raw BIDS dataset, the input a BIDS App reads.
  It can sit at any path inside a study; `sourcedata/<id>` is the convention, not a rule.
- **derivative**: what one BIDS App produced from one source dataset.
  A datalad dataset under the study's `derivatives/`, and the babs project that made it.
- **study**: short for [BIDS study](https://bids-specification.readthedocs.io/en/stable/common-principles.html#study-dataset), a dataset with `DatasetType: "study"`.
  The primary unit: one or more source datasets grouped with the derivatives made from them.
  mechababs operates on a study; it never creates one.
- **superstudy**: a BIDS study whose members are studies.
  An optional layer for running many studies at once, holding them as subdatasets and no raw data of its own.
- **member study**: a study inside a superstudy.
- **level**: study or superstudy, the scope a command runs at.
  Following datalad's superdataset convention, the hierarchy is a tree whose root is the outermost superstudy and whose leaves are the derivatives.
  "Up" is toward the root (the superstudy); "down" is toward the leaves (the derivatives).
  Memory hook: aggregation goes up, so each level's summary rolls up from the level below (derivative → study → superstudy).

## The campaign

- **campaign**: one processing run's recipe.
  A bundle of app configs, a cluster config, and a locked environment pinning mechababs and babs together.
  Lives in a campaign dir inside a study or superstudy; it is not a dataset.
- **label**: a campaign's name and identity.
  It names the campaign dir `.mechababs/campaigns/<label>/` and the venv inside it, and ends the name of every derivative the campaign produces.
  Selected by sourcing the campaign's `env.sh`, which sets `MECHABABS_CAMPAIGN`.
- **campaign dir**: `.mechababs/campaigns/<label>/` at a level.
  Holds the config copies, `pyproject.toml`, `uv.lock`, and the statefile or study catalog.
  At the configured level it also holds the venv and `env.sh`.
- **configured level**: the level `campaign init` ran at.
  The only level a campaign is operated from.
- **app config**: a YAML describing one [BIDS App](https://bids-apps.neuroimaging.io/) run: container, flags, `depends_on`.
  User-provided, and referred to on the CLI by its filename stem (`MRIQC-24.0.2`).
- **cluster config**: a YAML describing the site: SLURM resources, job preamble, scratch roots.
  User-provided, and usually private.
- **depends_on**: an ordering edge in an app config.
  The cell waits until the named producer's cell is merged.
  Ordering only, never wiring; wiring is babs's `input_datasets`.
- **producer**: the upstream cell a `depends_on` names.

## The reconciler

- **cell**: one (source dataset × app config) pair in a campaign.
  The unit the reconciler advances; it produces one derivative.
- **statefile**: `sourcedata+derivatives.tsv` in a study's campaign dir, one row per cell.
  A superstudy has none; it has a study catalog instead.
- **study catalog**: `studies+sourcedata.tsv` in a superstudy's campaign dir.
  One row per (member study, source dataset), each with a coarse lifecycle.
  The superstudy's counterpart to the statefile.
- **lifecycle**: `registered`, `active`, or `merged`, per study catalog row.
  Derived from the member study's statefile on each tick that scaffolds or merges one of its cells, not accumulated.
- **iterate**: one run of `mechababs iterate`.
  It visits every cell once, in order, and ticks each one that can advance, up to `--batch`.
- **tick**: one cell advancing by one transition: scaffold, submit, or merge.
  A cell that is passed over (merged, waiting, jobs still running, jobs failed) is not a tick and does not count against `--batch`.
- **transition**: a change of a cell's state: scaffold, submit, or merge.
  The change-making ones are recorded with `datalad run`.
- **inclusion**: the subject list a cell's jobs run over.
  Generated at scaffold and pinned in the campaign dir beside the statefile.
