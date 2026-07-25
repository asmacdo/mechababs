"""construct.py — the body of ``mechababs configure`` (campaign construction).

Runs from inside the campaign venv (invoked by ``cli.cmd_configure`` after
``bootstrap.sh`` built the environment: the datalad dataset, the vendored
babs/mechababs code pins, and the venv). It copies the requested pipeline +
cluster configs into the campaign's own ``pipelines/`` and ``clusters/`` (a config
given by path is copied in; a name already there resolves in place), vendors each
pipeline's container into ``code/<dir>``, and writes the mechababs config + the
mechababs state-file ledger. Configs live in the campaign so the one that produced
a run is committed alongside it; a starter set to copy from lives in
``code/mechababs/examples/``.

Re-runnable: container vendoring skips a ``code/<dir>`` already present, so a
reset (delete the ledger, re-run ``configure``) reuses the vendored containers.
The ledger guard lives in ``cli`` (refuses if the ledger exists, so
``add-dataset`` rows are never clobbered).
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from mechababs import __version__
from mechababs import state

# Invoke the campaign venv's datalad explicitly (not bare PATH) so construction
# uses the pinned tool whether or not the venv is activated.
DATALAD = str(Path(sys.prefix) / "bin" / "datalad")


def run(*cmd):
    """Run a command, echoing it; abort on non-zero exit."""
    print("+ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    subprocess.run([str(c) for c in cmd], check=True)


def capture(*cmd):
    """Run a command and return its stripped stdout (aborts on non-zero exit)."""
    return subprocess.run([str(c) for c in cmd], check=True,
                          capture_output=True, text=True).stdout.strip()


def container_dir(source):
    """The code/<dir> a container source is vendored into: its basename."""
    name = Path(source).name
    return name[:-4] if name.endswith(".git") else name


def pipeline_short(rel):
    """A pipeline's short_name: its filename stem. One identity for the ledger
    column prefix and the published derivative dir name ``<Tool>-<Ver>+<stage>``
    (e.g. ``fMRIPrep-25.2.5+anat``). No declared key — the filename IS the name."""
    return Path(rel).stem


def stage_config(campaign, kind, arg):
    """Bring one config into the campaign's ``kind`` dir; return ``(rel, copied)``.

    ``arg`` is either a path to a config file — copied into ``<kind>/`` so the
    campaign owns the config that produced a run (and reproduces from the campaign
    alone, not from the file's original location) — or the bare name of one already
    there, which resolves in place. ``rel`` is the campaign-relative path; ``copied``
    says whether a file was written, so the caller commits it. A copy of a file
    already in place is skipped. This is a pure filesystem op: committing a copied
    config is the caller's job (``build``).
    """
    dest_rel = f"{kind}/{Path(arg).name}"
    dest = campaign / dest_rel
    source = Path(arg)
    if source.is_file() and not (dest.is_file() and source.samefile(dest)):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, dest)
        return dest_rel, True
    if not dest.is_file():
        sys.exit(f"{kind[:-1]} config not found: {arg}")
    return dest_rel, False


def resolve_pipelines(campaign, pipeline_files):
    """Stage the requested pipeline configs into the campaign; return ``(rels, copied)``.

    Each entry is a path to copy in or the name of one already under ``pipelines/``
    (see ``stage_config``). ``rels`` are the campaign-relative paths, in order, and
    store the path (not the stem) so identity stays decoupled from location;
    ``copied`` is the subset freshly written (which ``build`` commits). Rejects a
    duplicate stem (two files that would merge column groups) before staging
    anything, so a rejected request never half-copies configs into the campaign.
    """
    seen = set()
    for arg in pipeline_files:
        short = pipeline_short(arg)
        if short in seen:
            sys.exit(f"duplicate pipeline {short!r} — pipeline names must be unique")
        seen.add(short)
    rels, copied = [], []
    for arg in pipeline_files:
        rel, was_copied = stage_config(campaign, "pipelines", arg)
        rels.append(rel)
        if was_copied:
            copied.append(rel)
    return rels, copied


def resolve_containers(campaign, pipeline_rels):
    """Unique container datasets to vendor: {dir: (source, ref)}, dir = basename.

    Deduped across pipelines sharing a source. ``ref`` pins a URL source; it is
    None for a local source. Rejects a dir mapped to conflicting (source, ref).
    """
    containers = {}
    for pipeline_rel in pipeline_rels:
        mechababs_cfg = (yaml.safe_load((campaign / pipeline_rel).read_text()) or {}).get("mechababs") or {}
        container = mechababs_cfg.get("container") or {}
        if not container:
            continue
        source, ref = container["source"], container.get("ref")
        vendor_dir = container_dir(source)
        if vendor_dir in containers and containers[vendor_dir] != (source, ref):
            sys.exit(f"container dir {vendor_dir!r} maps to conflicting (source, ref)")
        containers[vendor_dir] = (source, ref)
    return containers


def vendor_container(campaign, dir_, source, ref):
    """Vendor a container dataset into code/<dir> and pin it as a subdataset.

    A URL source is ``git clone --branch <ref>``'d then datalad-saved (pinned on
    the ref). A local source is ``datalad clone -d``'d (a relative path resolves
    against the campaign root). Skips a code/<dir> already present, so a reset
    re-run reuses it.
    """
    dest = campaign / "code" / dir_
    if dest.exists():
        print(f"container code/{dir_} already vendored — skipping", file=sys.stderr)
        return
    if "://" in source or source.startswith("git@"):
        run("git", "clone", "--branch", ref, source, dest)
        head = capture("git", "-C", dest, "rev-parse", "--short", "HEAD")
        run(DATALAD, "save", "--dataset", campaign, "--message",
            f"Vendor container {dir_} ({head})", dest)
    else:
        src = Path(source)
        if not src.is_absolute():
            src = (campaign / src).resolve()
        run(DATALAD, "clone", "-d", campaign, src, dest)


def build(campaign, pipeline_files, cluster, venv_rel, limit=None):
    """Construct the campaign: stage the configs, vendor containers, write config + ledger.

    ``campaign`` is a Path to the (already bootstrapped) campaign dataset;
    ``venv_rel`` is the venv's campaign-relative path (recorded for the job
    preamble); ``limit`` is the campaign-wide cap on each dataset's inclusion
    (None → all). Returns the pipeline short-names (filename stems).
    """
    pipelines, copied = resolve_pipelines(campaign, pipeline_files)
    shorts = [pipeline_short(rel) for rel in pipelines]
    cluster_rel, cluster_copied = stage_config(campaign, "clusters", cluster)
    if cluster_copied:
        copied.append(cluster_rel)

    # Commit any config copied in by path, so the campaign owns the exact config
    # that produced the run (and reproduces from it alone).
    if copied:
        run(DATALAD, "save", "--dataset", campaign, "--message",
            f"Copy in campaign configs ({', '.join(Path(c).name for c in copied)})",
            *[campaign / c for c in copied])

    for dir_, (source, ref) in resolve_containers(campaign, pipelines).items():
        vendor_container(campaign, dir_, source, ref)

    # The campaign is itself a BIDS study (it holds the cloned studies + produced
    # derivatives). Its dataset_description names mechababs as the GeneratedBy agent,
    # commit-bearing so the exact orchestrator is recoverable. (Matches the
    # OpenNeuroStudies study convention — BIDSVersion 1.9.0, DatasetType study.)
    desc = campaign / "dataset_description.json"
    desc.write_text(json.dumps({
        "Name": campaign.name,
        "BIDSVersion": "1.9.0",
        "DatasetType": "study",
        "GeneratedBy": [{"Name": "mechababs", "Version": __version__}],
    }, indent=2) + "\n")
    # BIDS nests by KIND (sourcedata/, derivatives/), so it can express neither a
    # study holding studies nor a set of retired derivative attempts. Both are real
    # campaign-level directories, so hide them from the validator. The study-of-
    # studies case is a gap to raise with BIDS rather than something .bidsignore
    # should have to answer — see docs/output_structure.md.
    bidsignore = campaign / ".bidsignore"
    bidsignore.write_text("studies/\nderivative-attempts/\n")
    run(DATALAD, "save", "--dataset", campaign, "--message",
        "Write campaign dataset_description.json (DatasetType study) + .bidsignore",
        desc, bidsignore)

    config = state.config_path(campaign)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(yaml.safe_dump(
        {"cluster": cluster_rel, "pipelines": pipelines, "venv": venv_rel, "limit": limit},
        sort_keys=False))
    run(DATALAD, "save", "--dataset", campaign, "--message",
        f"Write {state.CONFIG_FILENAME} (cluster + pipelines + venv)", config)

    # Gitignore the ledger lock here (not in bootstrap.sh): its filename derives
    # from a mechababs constant, so the entry belongs where that constant lives.
    # Idempotent — a reset re-run (delete ledger, re-configure) must not dup it.
    gitignore = campaign / ".gitignore"
    lines = gitignore.read_text().splitlines() if gitignore.exists() else []
    if state.LOCK_FILENAME not in lines:
        with gitignore.open("a") as f:
            f.write(f"{state.LOCK_FILENAME}\n")
        run(DATALAD, "save", "--dataset", campaign, "--message",
            f"Ignore the ledger lock ({state.LOCK_FILENAME})", gitignore)

    ledger = campaign / state.STATE_FILENAME
    ledger.write_text(state.initial_header(shorts))
    run(DATALAD, "save", "--dataset", campaign, "--message",
        f"Initialize {state.STATE_FILENAME} for pipelines {', '.join(shorts)}", ledger)

    return shorts
