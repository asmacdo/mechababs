"""scaffold.py — the first mutating transition: a cell from "not started" to "initialized".

One cell is (source dataset x app config). Scaffolding it means: work out which
subjects it should run, compose the babs config from the campaign's axes,
``babs init`` the derivative **into its final home** inside the study's
``derivatives/``, pin the requested subject list beside the statefile, and record
the derivative's path in the cell's ``babs`` column.

Nothing here commits. The whole verb runs inside a ``datalad run --explicit`` that
the dispatcher opens at the study (see ``dispatch.py``), so the commit — with the
verbatim command in it — is the orchestration record, and the four things this
writes (the derivative, the statefile, the inclusion pin, and the ``.gitmodules``
entry registering the derivative) are exactly what that run declares as outputs.

**Self-guarding, not gating.** A cell in the wrong state, or an unmerged producer,
is an *error* here — this verb is only ever reached because something decided the
cell was ready, so being wrong about that must be loud. The soft version — noting
"waiting on producer" and moving on — is the reconciler's, not this verb's.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from mechababs import campaign as campaign_mod
from mechababs import compose, select

# A study holding exactly one raw dataset conventionally parks it in a generic
# slot, whose directory name is not a dataset id — so there is no id to carry into
# the derivative's name, and none is needed (nothing to collide with).
GENERIC_SOURCEDATA_SLOTS = {"raw", "rawbids"}


def app_stem(app_config):
    """An app config's human-facing identity: its filename stem.

    ``app_config`` is the campaign-relative path (``bids-app-configs/X.yaml``) —
    that is the cell's identity; the stem is the derived form used for display,
    the derivative directory, and the inclusion pin.
    """
    return Path(app_config).stem


def source_id(source_dataset):
    """The source dataset's id: the last component of its study-relative path."""
    return Path(source_dataset).name


def derivative_name(source_dataset, app_config, label):
    """The derivative directory: ``<app stem>[+<id>]+<label>``.

    A cell is (source dataset x app config) in one campaign, and the name carries
    each part that can differ. ``<Tool>-<Ver>+<stage>`` alone would collide the
    moment a study holds two source datasets; a generic slot carries no id, since
    its directory name is not one. The campaign label comes last: a study
    accumulates campaigns, and without it a new campaign could not produce the
    same cell without first retiring the previous campaign's derivative.
    """
    stem = app_stem(app_config)
    dsid = source_id(source_dataset)
    name = stem if dsid in GENERIC_SOURCEDATA_SLOTS else f"{stem}+{dsid}"
    return f"{name}+{label}"


def derivative_path(source_dataset, app_config, label):
    """The derivative's STUDY-RELATIVE path — what the ``babs`` column records."""
    return f"derivatives/{derivative_name(source_dataset, app_config, label)}"


def inclusion_pin(study, label, source_dataset, app_config):
    """Where this cell's requested subject list is pinned, beside the statefile.

    Both halves of the cell's identity are *paths*, so neither is filename-safe as
    written. The sourcedata half is the whole study-relative path with ``/`` mapped
    to ``-`` (whole, so two datasets whose directories share a basename cannot
    collide); the app half is the config's stem. The result is a key mechababs
    constructs and never parses back, so the ambiguity of ``-`` inside a component
    costs nothing — and this is the only place the name is derived.
    """
    name = f"{source_dataset.replace('/', '-')}_{app_stem(app_config)}.csv"
    return campaign_mod.inclusions_dir(study, label) / name


def source_dataset_url(study, source_dataset):
    """The source dataset's URL, from the study's own ``.gitmodules``.

    Inputs are registered by URL, not local path, so the provenance babs records
    re-resolves off the machine that ran it. The section name depends on who built
    the study: OpenNeuroStudies (plain git) names it by dataset id, a datalad-built
    study names it by path. Both put the data at the same place; try both keys.
    """
    gitmodules = Path(study) / ".gitmodules"
    for name in (source_dataset, source_id(source_dataset)):
        out = subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(gitmodules),
                "--get",
                f"submodule.{name}.url",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    sys.exit(
        f"no submodule url for {source_dataset} in {gitmodules}\n"
        "The source dataset has to be a registered subdataset of the study — "
        "that registration is what says where the raw data came from."
    )


def resolve_container_ds(study, container):
    """What to hand ``babs init --container-ds``.

    A URL is passed through verbatim (what a production config carries). Anything
    else is a path to a container dataset; a relative one resolves against the
    **study root**, so a config can name a sibling checkout without baking an
    absolute path into a committed file.

    There is no vendoring step: the container dataset is an input babs installs,
    not something mechababs copies into the campaign first.
    """
    source = str(container["source"])
    if "://" in source or source.startswith("git@"):
        return source
    path = Path(source)
    return str(path if path.is_absolute() else (Path(study) / path).resolve())


def output_ria_url(project_root, app_config_data):
    """The clone source a downstream cell registers for its upstream input: the
    producing babs project's output RIA, addressed through the ``data`` alias babs
    writes there at init.

    The alias keeps this layout-agnostic (no dataset-id lookup). The abspath is
    fine: it is consumed at compose time and baked into the derivative's own babs
    config by babs — never written into the git-tracked statefile.
    """
    ria_rel = app_config_data.get("output_ria_path", "output_ria")
    return f"ria+file://{Path(project_root).resolve()}/{ria_rel}#~data"


def require_producer_merged(rows, row):
    """The ``depends_on`` gate — **ordering only**, and nothing else.

    ``depends_on`` carries no kind and wires nothing: this cell is not scaffolded
    until the producer's cell is merged. The producer is a row lookup in this shard
    (the same source dataset's upstream-app row), so an edge can never cross studies.
    Input wiring is ``input_datasets``' (``resolve_input_origins``), which this never
    reads; the two typically name the same producer, on purpose.
    """
    upstream = row.get("depends_on") or ""
    if not upstream:
        return
    producer = campaign_mod.find_cell(rows, row["source_dataset"], upstream)
    if not producer.get("merged"):
        sys.exit(
            f"{row['source_dataset']} / {app_stem(row['app_config'])} depends on "
            f"{app_stem(upstream)}, which is not merged yet.\n"
            "A dependent cell is scaffolded only after its producer's results are "
            "merged — the reconciler waits for that; this verb refuses."
        )


def find_producer(rows, row, stem):
    """The cell in this shard whose app config has ``stem``, for this source dataset.

    ``None`` when nothing matches: an ``input_datasets`` key naming no cell is an
    input from outside the campaign (raw BIDS, a precomputed derivative), which
    carries its own ``origin_url`` in the config and is left alone.
    """
    for candidate in rows:
        if candidate is row or candidate["source_dataset"] != row["source_dataset"]:
            continue
        if app_stem(candidate["app_config"]) == stem:
            return candidate
    return None


def resolve_input_origins(study, label, rows, row, app_config_data):
    """Resolve every ``input_datasets`` entry that names a producer in this campaign.

    Input wiring is driven **solely** by the app config's ``input_datasets`` — the
    declaration the user writes for babs's sake. A key that names another cell's app
    is that cell's output being consumed, so it is wired to the producing babs
    project's merged output store; the YAML cannot carry that URL, because the store
    does not exist until the producer has run.

    ``depends_on`` is not consulted here. Where both are declared (the normal case)
    its gate has already refused an unmerged producer; the check below is what
    catches a config that declares the wiring and forgets the ordering edge, where
    handing babs an input with no origin would be the quieter, worse failure.

    Returns ``{input key: origin url}``, empty for a cell with no in-campaign inputs.
    """
    origins = {}
    for key in app_config_data.get("input_datasets") or {}:
        producer = find_producer(rows, row, key)
        if producer is None:
            continue
        if not producer.get("merged"):
            sys.exit(
                f"{row['source_dataset']} / {app_stem(row['app_config'])} takes "
                f"{key} as an input, and the cell that produces it is not merged "
                "yet.\nThere is no output store to wire until it is — declare the "
                "ordering with `depends_on` so the reconciler waits for it."
            )
        producer_config = yaml.safe_load(
            (
                campaign_mod.campaign_dir(study, label) / producer["app_config"]
            ).read_text()
        )
        origins[key] = output_ria_url(Path(study) / producer["babs"], producer_config)
    return origins


def resolve_inclusion(study, label, row, app_config_data, limit):
    """This cell's subject list: the pin if one is already there, else generated.

    A pin present is used **as-is** and selection is skipped entirely — that is the
    smoke-test affordance (hand-write a one-row file before the first iterate and the
    whole pipeline runs on one subject), and it is also how a re-scaffold reuses
    exactly what the first attempt requested.

    Otherwise the app's declarative ``selection`` rule is applied to the study's own
    per-subject metadata, formatted to the cell's ``processing_level`` so the list
    and the level babs runs at cannot disagree, and capped by the campaign's
    ``limit``. The pin records what was *requested*; babs writes its own
    ``processing_inclusion.csv`` of what it could actually run, and the diff is what
    catches a selected subject the data does not have.
    """
    pin = inclusion_pin(study, label, row["source_dataset"], row["app_config"])
    if pin.exists():
        print(f"  using the inclusion already pinned at {pin}", file=sys.stderr)
        return pin

    mechababs_cfg = app_config_data.get(compose.MECHABABS_NAMESPACE) or {}
    if "selection" not in mechababs_cfg:
        sys.exit(
            f"app {app_stem(row['app_config'])} declares no `mechababs.selection` "
            f"and has no pinned inclusion at {pin} — one of the two has to define "
            f"the job universe (`selection: {{}}` is the pass-through rule)."
        )

    pin.parent.mkdir(parents=True, exist_ok=True)
    tsv_text, _ = select.read_study_metadata(study)
    select.generate_inclusion(
        tsv_text,
        mechababs_cfg["selection"] or {},
        pin,
        processing_level=row["processing_level"],
        limit=limit,
    )
    return pin


def babs_init_command(study, row, project, app_config_data, inclusion, babs_config):
    """The ``babs init`` argv for this cell, with study-relative paths.

    Study-relative because the run's cwd is the study: the recorded command has to
    re-execute somewhere else, and an absolute derivative path would not.
    (``--container-ds`` and ``--container-config`` are the exceptions — a container
    source is a URL or an outside-the-study checkout, and the composed config is a
    tempfile, so neither has a study-relative form.)
    """
    container = (app_config_data.get(compose.MECHABABS_NAMESPACE) or {}).get(
        "container"
    )
    if not container:
        sys.exit(
            f"app {app_stem(row['app_config'])} declares no "
            f"`mechababs.container` — scaffold has no image to give babs."
        )
    cmd = [
        campaign_mod.babs_bin(),
        "init",
        project,
        "--container-ds",
        resolve_container_ds(study, container),
        "--container-name",
        container["name"],
        "--container-config",
        str(babs_config),
        "--processing-level",
        row["processing_level"],
        "--queue",
        "slurm",
    ]
    if inclusion is not None:
        cmd += ["--list-sub-file", str(inclusion.relative_to(study))]
    return cmd


def scaffold(study, label, source_dataset, app_config):
    """Advance one cell from "not started" to "initialized". Returns its ``babs`` path.

    Called inside the dispatcher's ``datalad run``; commits nothing itself.
    """
    study = Path(study)
    campaign = campaign_mod.campaign_dir(study, label)
    config = yaml.safe_load(campaign_mod.config_path(study, label).read_text()) or {}

    rows = campaign_mod.read_state(study, label)
    row = campaign_mod.find_cell(rows, source_dataset, app_config)
    if row.get("babs"):
        sys.exit(
            f"{source_dataset} / {app_stem(app_config)} is already scaffolded at "
            f"{row['babs']}.\n"
            "Scaffold is the not-started transition; to redo this cell, retire the "
            "derivative first — that resets the cell in the same act."
        )
    if not row.get("processing_level"):
        sys.exit(
            f"{source_dataset} has no processing_level in the statefile — "
            "add-dataset derives it from the study's metadata, so a blank one "
            "means that sniff never happened."
        )

    app_config_data = yaml.safe_load((campaign / app_config).read_text()) or {}
    cluster_config_data = (
        yaml.safe_load((campaign / config["cluster"]).read_text()) or {}
    )

    # The gate first: nothing is written before we know the cell may proceed. The
    # two are independent — ordering is `depends_on`'s, wiring is `input_datasets`'.
    require_producer_merged(rows, row)
    input_origins = resolve_input_origins(study, label, rows, row, app_config_data)

    project = derivative_path(source_dataset, app_config, label)
    print(
        f"\n=== scaffold {source_dataset} / {app_stem(app_config)} -> {project} ===",
        file=sys.stderr,
    )
    (study / project).parent.mkdir(parents=True, exist_ok=True)

    inclusion = resolve_inclusion(
        study, label, row, app_config_data, config.get("limit")
    )

    # The composed config is derived from committed inputs and babs keeps its own
    # resolved copy inside the derivative, so a tempfile loses nothing and keeps the
    # run's declared outputs to the four the transition owns. The venv path goes
    # into every job script, so it names the level the campaign is operated from: a
    # member of a super-campaign has no environment of its own.
    operated_at = campaign_mod.operated_level(study, label)
    with tempfile.TemporaryDirectory() as tmp:
        babs_config = compose.write_babs_config(
            Path(tmp) / "babs-config.yaml",
            app_config_data,
            cluster_config_data,
            source_dataset_url(study, source_dataset),
            input_origins=input_origins,
            campaign_venv=campaign_mod.venv_path(operated_at, label),
        )
        cmd = babs_init_command(
            study, row, project, app_config_data, inclusion, babs_config
        )
        print("+ " + " ".join(str(c) for c in cmd), file=sys.stderr)
        subprocess.run([str(c) for c in cmd], cwd=str(study), check=True)

    # The cell's durable fact: a babs project exists, and where. Its presence is
    # what routes the next iterate to the active branch instead of back through here.
    row["babs"] = project
    campaign_mod.write_state(study, label, rows)
    return project
