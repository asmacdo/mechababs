"""iterate.py — the campaign reconciler tick.

One `iterate` advances each (dataset, pipeline) cell by AT MOST ONE transition,
routing on which ledger columns are filled:
  - `_babs` empty            -> SCAFFOLD: generate the inclusion into the campaign
                                pin (or reuse it; a chained cell skips it), compose
                                the babs config, `babs init` (NO submit), record
                                `_babs` (the project-root path).
  - `_babs` set, not merged  -> ACTIVE: read `babs status --json`, take the next
                                step (deploy more / skip / merge / flag-failed).
  - `_babs-merged` set       -> done, skipped (no babs query).

The ACTIVE step is decided from `babs status --json` counts (the babs_status
decide seam) and dispatched through ITERATE_ACTIONS. mechababs shells out to babs /
merge_config.py and imports the select and babs_status modules. `--dry-run` runs
read-only steps (e.g. `babs status`) for real and prints the mutating commands
without running them.
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import yaml

from datalad.api import Dataset

from mechababs import babs_status
from mechababs import construct
from mechababs import select
from mechababs import state
from mechababs.utils import datalad_save_scope, locked

# Repo root of the vendored mechababs — holds the top-level helper scripts iterate
# still shells out to (merge_config); selection is now the in-package select module.
MECHABABS_ROOT = Path(__file__).resolve().parent.parent
MERGE_CONFIG_SCRIPT = MECHABABS_ROOT / "merge_config.py"


def run(cmd, *, dry_run, cwd=None, env=None):
    """Run a command (list of args), or just print it under dry_run.

    `env`, if given, is merged over the current environment for the child."""
    shown = " ".join(str(c) for c in cmd) + (f"   (cwd={cwd})" if cwd else "")
    if dry_run:
        print(f"DRY-RUN  {shown}", file=sys.stderr)
        return
    print(f"+ {shown}", file=sys.stderr)
    subprocess.run(
        [str(c) for c in cmd],
        cwd=cwd,
        check=True,
        env={**os.environ, **env} if env else None,
    )


def datalad_duct(cmd, *, dataset, message, log_prefix, dry_run):
    """Run a mutating shell-out under duct (resource logging) inside a `datalad run`,
    so the duct logs land tracked in the campaign.

    No inputs/outputs are declared (datalad run saves whatever changed, mirroring
    datalad_save_scope's since-save). The duct output prefix is passed via the
    DUCT_OUTPUT_PREFIX env var, NOT duct's -p flag: datalad run runs its own
    {placeholder} substitution over the command and would choke on duct's
    {datetime}/{pid} format fields (same lesson as the babs add-duct branch's
    764d5ae). Read-only babs shell-outs (`babs status`) are NOT wrapped — only the
    mutating steps (init now; submit/merge later)."""
    duct_cmd = ["duct", *[str(c) for c in cmd]]
    run(
        ["datalad", "run", "-d", str(dataset), "-m", message, *duct_cmd],
        dry_run=dry_run,
        cwd=dataset,
        env={"DUCT_OUTPUT_PREFIX": log_prefix},
    )


# TODO move to util
def warn_if_no_tmux():
    """Prompt before a long login-node run that a disconnect would kill (cf lib.sh).

    Only meaningful interactively; a non-tty caller (CI / containers / pipes) can't
    answer, so skip the prompt there rather than block on EOF. MECHABABS_SKIP_TMUX_CHECK
    bypasses it deterministically for automation (the e2e harness sets it): the prompt
    reads inherited stdin, so when a driver subprocess-es iterate a stray keystroke
    buffered during a long tick can be consumed as the answer and abort the run.
    """
    if os.environ.get("MECHABABS_SKIP_TMUX_CHECK"):
        return
    if os.environ.get("TMUX") or os.environ.get("STY"):
        return
    if not sys.stdin.isatty():
        print(
            "Note: not inside tmux/screen (non-interactive — continuing).",
            file=sys.stderr,
        )
        return
    ans = input(
        "Not inside tmux/screen — a disconnect will kill this run. Continue anyway? [y/N] "
    )
    if not ans.lower().startswith("y"):
        sys.exit("Aborting.")


# TODO move to util
def read_config(campaign):
    """The mechababs config -> {cluster, pipelines: [file, ...]} (campaign-relative paths).

    A pipeline's short_name is its filename stem (construct.pipeline_short) — the
    ledger column prefix and the derivative dir name — so the list of paths is the
    only pipeline state; the identifier is derived, never declared."""
    return yaml.safe_load(state.config_path(campaign).read_text())


def assert_venv_tools(campaign, cfg):
    """Guard: babs/mechababs/duct on PATH must resolve inside the campaign venv.

    A stray active venv silently substitutes a different, unrecorded babs while
    outputs stay attributed to the pinned commit; per-campaign vendoring only
    means anything if the *pinned* tools run.
    """
    venv = (campaign / cfg["venv"]).resolve()
    for tool in ("babs", "mechababs", "duct"):
        found = shutil.which(tool)
        if not (found and Path(found).resolve().is_relative_to(venv)):
            sys.exit(
                f"{tool} resolves to {found or 'nothing'}, not under the campaign venv "
                f"{venv} — activate the campaign venv (a stray venv on PATH would run an "
                f"unpinned {tool}, silently breaking the campaign's provenance pin)"
            )


def dataset_id(url):
    """ds000001 from .../ds000001.git or .../ds000001."""
    name = Path(url).name
    return name[:-4] if name.endswith(".git") else name


def container_dir(source):
    """The code/<dir> a container source is vendored into: its basename."""
    name = Path(source).name
    return name[:-4] if name.endswith(".git") else name


def resolve_container_ds(campaign, container):
    """The --container-ds for babs init: the container vendored at code/<dir>,
    where <dir> is the source's basename. `mechababs configure` vendors every
    container there regardless of source (a URL, or a local dataset like a
    hand-built shim), so iterate needn't know how it was built.
    """
    return campaign / "code" / container_dir(container["source"])


def study_sourcedata_url(study, ds_id):
    """The raw dataset's URL, read from the cloned study's `.gitmodules`
    `sourcedata/<id>` entry — the study's own record of its input, so babs
    registers the input by that URL rather than a campaign-local path.

    The `.gitmodules` section name depends on who built the study: OpenNeuroStudies
    (plain git) names it by dataset id (`[submodule "<id>"]`), while a datalad-built
    study names it by path (`[submodule "sourcedata/<id>"]`). Both put the same file
    at `sourcedata/<id>`; try the id key first, then the path key.
    """
    gitmodules = study / ".gitmodules"
    for name in (ds_id, f"sourcedata/{ds_id}"):
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
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    sys.exit(f"no sourcedata/{ds_id} submodule url in {gitmodules}")


def babs_project(campaign, row, short):
    """The babs-project root for a cell — the campaign-relative path in `_babs`,
    which `babs status|submit|merge` take positionally."""
    return campaign / row[f"{short}_babs"]


def selected_pipelines(campaign, cfg):
    """{short_name: loaded pipeline cfg} for every pipeline in this campaign."""
    return {
        construct.pipeline_short(pf): yaml.safe_load((campaign / pf).read_text())
        for pf in cfg["pipelines"]
    }


def chain_edges(pipeline_cfg, selected):
    """The in-campaign upstream stages this pipeline consumes: its `input_datasets`
    keys that name another selected pipeline's short_name.

    A key that matches a selected short_name IS a chained edge — its origin_url is
    intentionally absent from the YAML (the upstream RIA doesn't exist until it has
    run + merged), so the name match is the only signal knowable at author time. A
    key matching no selected pipeline is an external input (raw BIDS, or a
    precomputed derivative from outside the campaign) that carries its own
    origin_url."""
    return [k for k in (pipeline_cfg.get("input_datasets") or {}) if k in selected]


def output_ria_url(project_root, producer_cfg):
    """The clone source a downstream stage registers as its input's origin_url: the
    producing babs project's output RIA, addressed through the `data` alias babs
    writes there at init.

    The alias is layout-agnostic (no dataset-id lookup, no assumption about the
    analysis/RIA nesting); the RIA's location is per-pipeline babs config
    (`output_ria_path`, babs default `output_ria`). Validated to clone + retrieve
    annex content on a real produced derivative. The abspath is fine here: it's a
    per-run RIA source consumed at compose time and baked into the derivative's own
    babs config by babs — never recorded in the git-tracked ledger."""
    ria_rel = producer_cfg.get("output_ria_path", "output_ria")
    return f"ria+file://{Path(project_root).resolve()}/{ria_rel}#~data"


@dataclass
class _ScaffoldCtx:
    """The paths + config one scaffold needs, gathered once and threaded to its steps."""

    campaign: Path
    cfg: dict
    short: str
    ds_id: str
    processing_level: str
    study: Path
    origin_url: str
    pipeline_path: Path
    cluster_path: Path
    pipeline_cfg: dict
    container: dict
    campaign_venv: str
    project_root: Path


def _scaffold_context(campaign, cfg, row, short, pipeline_file):
    """Gather the scaffold's inputs, validating the prerequisites (processing_level,
    venv). The derivative is produced in its final home inside the cloned study; the
    babs-project root IS the derivative dataset, named by the pipeline's short_name."""
    ds_id = row["dataset_id"]
    processing_level = row.get("processing_level")
    if not processing_level:
        sys.exit(
            f"processing_level not set for {ds_id} — set it in the ledger "
            f"(add-dataset derives it; a blank means the metadata fetch failed)"
        )
    venv_rel = cfg.get("venv")
    if not venv_rel:
        sys.exit(
            f"{state.CONFIG_FILENAME} has no 'venv' — run `mechababs configure` first"
        )
    study = campaign / "studies" / f"study-{ds_id}"
    pipeline_path = campaign / pipeline_file
    pipeline_cfg = yaml.safe_load(pipeline_path.read_text())
    return _ScaffoldCtx(
        campaign=campaign,
        cfg=cfg,
        short=short,
        ds_id=ds_id,
        processing_level=processing_level,
        study=study,
        origin_url=study_sourcedata_url(study, ds_id),
        pipeline_path=pipeline_path,
        cluster_path=campaign / cfg["cluster"],
        pipeline_cfg=pipeline_cfg,
        container=pipeline_cfg["mechababs"]["container"],
        campaign_venv=str(campaign / venv_rel),
        project_root=study / "derivatives" / short,
    )


def _resolve_chained_inputs(ctx, row):
    """Gate the cell on every upstream stage it consumes having merged, and resolve
    each upstream's output-RIA URL to inject as that input's origin_url (the YAML
    leaves it blank — the RIA doesn't exist until the upstream runs). Returns
    (edges, input_origins) — `edges` may be empty (unchained) — or None if an
    upstream isn't merged yet, so the reconciler skips this cell (a later tick
    retries). No filesystem mutation happens before this gate."""
    selected = selected_pipelines(ctx.campaign, ctx.cfg)
    edges = chain_edges(ctx.pipeline_cfg, selected)
    unmet = [u for u in edges if not row.get(f"{u}_babs-merged")]
    if unmet:
        print(
            f"  {ctx.ds_id}/{ctx.short}: waiting on upstream {', '.join(unmet)} "
            f"(not merged) — skipping this tick",
            file=sys.stderr,
        )
        return None
    input_origins = {
        u: output_ria_url(ctx.campaign / row[f"{u}_babs"], selected[u]) for u in edges
    }
    return edges, input_origins


def _resolve_inclusion(ctx, edges, *, dry_run):
    """The per-(dataset,pipeline) inclusion pin under .mechababs/inclusions/ (or None
    for a chained cell). The pin is the interface — iterate advances whichever cell is
    next, so a runtime inclusion could land on the wrong cell:
      chained -> None; babs derives the job set by intersecting the inputs (raw ∩ each
                 upstream output), dropping any subject an upstream didn't produce. It
                 intersects on subject presence, not datatype, so a downstream may
                 still get one its own rule would exclude (e.g. BOLD-less for minimal);
                 those jobs fail fast. TODO: run our own selection to trim them.
      pinned  -> reuse it (prior tick, or hand-curated).
      else    -> select generates it from the study TSV + the pipeline's selection
                 rule ({} = all subjects), capped by limit.
    The pin records what we requested; babs writes its own processing_inclusion.csv,
    whose diff flags a requested subject the data lacks."""
    if edges:
        print(
            "  chained cell — no inclusion; babs intersects its inputs", file=sys.stderr
        )
        return None
    pin = (
        ctx.campaign
        / state.MECHABABS_DIR
        / "inclusions"
        / f"{ctx.ds_id}_{ctx.short}.csv"
    )
    if pin.exists():
        print(f"  using pinned inclusion {pin}", file=sys.stderr)
        return pin
    mechababs_cfg = ctx.pipeline_cfg["mechababs"]
    if "selection" not in mechababs_cfg:
        sys.exit(
            f"pipeline {ctx.short} has no `mechababs.selection` and no pinned "
            f"inclusion at {pin} — one is needed to define the job universe "
            f"(use `selection: {{}}` for pass-through)"
        )
    rule = mechababs_cfg["selection"] or {}
    # processing_level (from the ledger) formats the inclusion to match the level babs
    # runs at, so the two never disagree.
    limit = ctx.cfg.get("limit")
    if dry_run:
        print(
            f"DRY-RUN  select.generate_inclusion for {ctx.ds_id}/{ctx.short} "
            f"(rule={rule}, processing_level={ctx.processing_level}, "
            f"limit={limit}) -> {pin}",
            file=sys.stderr,
        )
    else:
        pin.parent.mkdir(parents=True, exist_ok=True)
        tsv_text, _ = select.read_study_metadata(ctx.study)
        select.generate_inclusion(
            tsv_text, rule, pin, processing_level=ctx.processing_level, limit=limit
        )
    return pin


def _compose_babs_config(ctx, input_origins, *, dry_run):
    """Compose the babs container-config (pipeline x cluster x dataset-url), resolving
    the venv placeholder with the campaign venv and injecting each chained input's
    resolved upstream output-RIA URL. Written to a tracked campaign path
    (.mechababs/babs-init-config/), not a tempdir, so the babs-init datalad-run command
    references it relatively. (babs consumes it and stores its own altered copy at
    .babs/, so the config is recorded a few times over — the redundancy was always
    there, now it's visible/tracked.) Returns the config path."""
    babs_config = (
        ctx.campaign
        / state.MECHABABS_DIR
        / "babs-init-config"
        / f"{ctx.ds_id}_{ctx.short}.yaml"
    )
    merge_cmd = [
        "python3",
        str(MERGE_CONFIG_SCRIPT),
        "--pipeline",
        str(ctx.pipeline_path),
        "--cluster",
        str(ctx.cluster_path),
        "--dataset-url",
        ctx.origin_url,
        "--campaign-venv",
        ctx.campaign_venv,
    ]
    for key, url in input_origins.items():
        merge_cmd += ["--input-origin", f"{key}={url}"]
    if dry_run:
        run(merge_cmd + [">", str(babs_config)], dry_run=True)
    else:
        babs_config.parent.mkdir(parents=True, exist_ok=True)
        with open(babs_config, "w") as f:
            subprocess.run(merge_cmd, check=True, stdout=f)
    return babs_config


def _babs_init(ctx, inclusion, babs_config, *, dry_run):
    """Commit the mechababs inputs, then `babs init` under duct (scaffold only, NO
    submit). babs init runs under `datalad run` (via datalad_duct), which needs a clean
    tree to detect the command's changes — so commit the inclusion pin + composed
    config first. Paths are campaign-RELATIVE so the recorded command is portable (the
    run's cwd is the campaign); --list-sub-file (when given) defines the job universe,
    which babs records as the derivative's code/processing_inclusion.csv — a chained
    cell omits it, letting babs intersect the inputs instead."""
    inputs = [str(babs_config)] + ([str(inclusion)] if inclusion is not None else [])
    run(
        [
            "datalad",
            "save",
            "-d",
            ctx.campaign,
            "-m",
            f"scaffold inputs {ctx.ds_id}/{ctx.short}",
            *inputs,
        ],
        dry_run=dry_run,
    )

    babs_init = [
        "babs",
        "init",
        str(ctx.project_root.relative_to(ctx.campaign)),
        "--container-ds",
        str(
            resolve_container_ds(ctx.campaign, ctx.container).relative_to(ctx.campaign)
        ),
        "--container-name",
        ctx.container["name"],
        "--container-config",
        str(babs_config.relative_to(ctx.campaign)),
        "--processing-level",
        ctx.processing_level,
        "--queue",
        "slurm",
    ]
    if inclusion is not None:
        babs_init += ["--list-sub-file", str(inclusion.relative_to(ctx.campaign))]
    datalad_duct(
        babs_init,
        dataset=ctx.campaign,
        message=f"scaffold {ctx.ds_id}/{ctx.short}: babs init",
        log_prefix=f".duct-logs/{ctx.ds_id}/{ctx.short}/babs-init_{{datetime}}-{{pid}}_",
        dry_run=dry_run,
    )


def scaffold(campaign, cfg, row, short, pipeline_file, *, dry_run):
    """Advance one (dataset, pipeline) from 'not started' to 'initialized'.

    Returns the ledger update ({<short>_babs: project-root path}); the caller applies
    it only on a real (non-dry) run. Returns None if a chained upstream isn't merged
    yet (the cell can't scaffold this tick). Both mechababs-owned inputs — the
    inclusion pin and the composed babs config — live under `.mechababs/`
    (`inclusions/`, `babs-init-config/`) so the babs-init `datalad run` record
    references portable relative paths.
    """
    ctx = _scaffold_context(campaign, cfg, row, short, pipeline_file)
    gate = _resolve_chained_inputs(ctx, row)
    if gate is None:
        return None
    edges, input_origins = gate

    print(
        f"\n=== scaffold {ctx.ds_id} / {short} -> {ctx.project_root.name} ===",
        file=sys.stderr,
    )
    if not dry_run:
        ctx.project_root.parent.mkdir(parents=True, exist_ok=True)

    inclusion = _resolve_inclusion(ctx, edges, dry_run=dry_run)
    babs_config = _compose_babs_config(ctx, input_origins, dry_run=dry_run)
    _babs_init(ctx, inclusion, babs_config, dry_run=dry_run)

    # Ledger: babs = the babs-project root, campaign-relative — the handle later ticks
    # drive babs against (`babs status|submit|merge <path>`). Its presence answers both
    # "scaffolded?" and "where?".
    return {f"{short}_babs": str(ctx.project_root.relative_to(campaign))}


# --- ACTIVE-cell transitions: (campaign, cfg, row, short, *, dry_run) -> updates ---


def submit(campaign, cfg, row, short, *, dry_run):
    """SUBMIT: deploy more jobs. Writes no column — submit-state is babs's."""
    run(["babs", "submit", babs_project(campaign, row, short)], dry_run=dry_run)
    return {}


def merge(campaign, cfg, row, short, *, dry_run):
    """MERGE: all jobs done -> babs merge -> pull results into the campaign -> finished.

    `babs merge` leaves the merged results in the output RIA (content stays there
    by design); `datalad update` fast-forwards the babs project's working tree to
    the merged branch so the campaign actually holds the produced derivative while
    the RIA store is still live.
    """
    proj = babs_project(campaign, row, short)
    run(["babs", "merge", proj], dry_run=dry_run)
    run(
        ["datalad", "update", "--how", "merge", "-s", "output", "-d", proj],
        dry_run=dry_run,
    )
    return {f"{short}_babs-merged": "true"}


def skip(campaign, cfg, row, short, *, dry_run):
    """SKIP: jobs still in flight -> nothing to do this tick."""
    return None


def fail(campaign, cfg, row, short, *, dry_run):
    """FAIL: some jobs failed -> flag and leave unmerged (re-surfaces each tick;
    retry/policy deferred, #66)."""
    proj = babs_project(campaign, row, short)
    print(
        f"  {row['dataset_id']}/{short}: jobs FAILED — not merging; needs "
        f"attention (`babs status {proj}`)",
        file=sys.stderr,
    )
    return None


# decide()'s return values mapped to handlers; each has the uniform
# (campaign, cfg, row, short, *, dry_run) signature and returns the ledger update
# to apply (or None/{} for no-op), so run_iterate dispatches ITERATE_ACTIONS[action].
ITERATE_ACTIONS = {
    "submit": submit,
    "skip": skip,
    "merge": merge,
    "fail": fail,
}


def decide_action(campaign, row, short):
    """Decide a cell's next transition from its ledger columns (read-only — the caller
    names the campaign commit after the returned action, then dispatches it):
      empty `_babs`  -> 'scaffold' (never started);
      else           -> read `babs status --json` and decide submit/skip/merge/fail
                        via babs_status.decide."""
    if not row.get(f"{short}_babs"):
        return "scaffold"
    ds = row["dataset_id"]
    proj = babs_project(campaign, row, short)
    print(f"\n=== status {ds}/{short} ({proj.name}) ===", file=sys.stderr)

    status = babs_status.read_status(proj)
    action = babs_status.decide(status)
    print(f"  {ds}/{short}: {status} -> {action}", file=sys.stderr)
    return action


def run_iterate(campaign, *, batch, dry_run):
    """One tick: advance each (dataset, pipeline) cell by at most one transition.

    Routes on the cell's ledger columns: empty `_babs` -> scaffold; `_babs` set and
    not `_babs-merged` -> the active handler; merged -> skip. The lock
    is held across the whole batch (single writer), and each advanced cell is saved
    as it lands so a long or interrupted tick still records progress.
    """
    cfg = read_config(campaign)
    if not dry_run and cfg.get("venv"):
        assert_venv_tools(campaign, cfg)
    pipelines = cfg["pipelines"]
    with locked(campaign):
        cols = state.header(campaign)
        rows = state.read_rows(campaign)

        # Work = every not-yet-merged cell; the loop derives its action from the
        # same columns (empty `_babs` -> scaffold, else the babs-status decision).
        work = []
        for row in rows:
            for pf in pipelines:
                short = construct.pipeline_short(pf)
                if not row.get(f"{short}_babs-merged"):
                    work.append((row, short, pf))
        if batch is not None:
            work = work[:batch]
        if not work:
            print("iterate: nothing to do (every pipeline is merged).", file=sys.stderr)
            return

        campaign_ds = Dataset(campaign)
        for row, short, pf in work:
            ds_id = row["dataset_id"]
            action = decide_action(campaign, row, short)
            if action == "scaffold":
                transition = partial(
                    scaffold, campaign, cfg, row, short, pf, dry_run=dry_run
                )
            else:
                transition = partial(
                    ITERATE_ACTIONS[action], campaign, cfg, row, short, dry_run=dry_run
                )
            # Every transition is one recursive node on the campaign: the scope opens
            # clean, spans the transition's work + the ledger row, and its
            # save(since=, recursive=True) bumps the gitlink up each level of the nest
            # (derivative -> study -> campaign). scaffold and merge mutate the nest;
            # submit/skip mutate nothing trackable, so their scope saves nothing (a
            # no-op). The clean-in guard enforces the between-transitions clean-tree.
            with datalad_save_scope(
                campaign_ds,
                f"{action} {ds_id}/{short}",
                recursive=True,
                dry_run=dry_run,
            ):
                updates = transition()
                if updates and not dry_run:
                    row.update(updates)
                    state.write_rows(campaign, cols, rows)

        if dry_run:
            print(f"\nDRY-RUN: would advance {len(work)} cell(s).", file=sys.stderr)
        else:
            print(f"\niterate: processed {len(work)} cell(s).", file=sys.stderr)
