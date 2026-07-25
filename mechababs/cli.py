"""mechababs — the operate-side CLI (configure / add-dataset / iterate / …).

Runs inside a campaign's venv (built by bootstrap.sh). ``configure`` binds an
ordered pipeline-set to a cluster (the mechababs config + the ledger) from inside
that venv; the other subcommands mutate or advance the state-file ledger. The
environment half of the bootstrap — datalad dataset, vendored code pins, venv —
is bootstrap.sh's job.
"""

import argparse
import subprocess
import sys
from pathlib import Path

from mechababs import __version__
from mechababs import construct
from mechababs import guard
from mechababs import iterate as iterate_mod
from mechababs import retire as retire_mod
from mechababs import select
from mechababs import state
from mechababs import status as status_mod
from mechababs import utils


def _ensure_campaign(args):
    """Resolve --campaign-path and confirm it is a campaign (i.e. has a ledger)."""
    campaign = args.campaign_path.resolve()
    if not state.state_path(campaign).is_file():
        sys.exit(f"not a campaign (no {state.STATE_FILENAME}): {campaign}")
    return campaign


def cmd_configure(args):
    """Configure the campaign: bind an ordered pipeline-set to a cluster.

    Runs from inside the campaign venv. bootstrap.sh established the
    preconditions this checks: the path is a datalad dataset with code/mechababs
    + code/babs registered, and THIS process runs from the campaign's own .venv —
    which is how we know the pinned code (not some ambient install) is executing.
    This is the guard that kills the wrong-babs bug. Then construct.build copies
    the named configs into the campaign, vendors the pipelines' containers, and
    writes the config + the ledger.
    """
    campaign = args.campaign_path.resolve()

    # Look like a campaign skeleton bootstrap.sh built?
    if not (campaign / ".datalad").is_dir():
        sys.exit(f"not a datalad dataset: {campaign}")
    for sub in ("code/mechababs", "code/babs"):
        if not (campaign / sub).is_dir():
            sys.exit(f"not a campaign skeleton (missing {sub}): {campaign}")

    # Provenance guard: the code pins must match what the campaign records.
    guard.require_clean_pins(campaign)

    # The PATH guard, the whole point: are we the campaign venv's python? Its
    # sys.prefix is <campaign>/.venv. If not, an ambient mechababs is running and
    # would scaffold with the wrong (unpinned) babs — refuse.
    venv = (campaign / ".venv").resolve()
    prefix = Path(sys.prefix).resolve()
    if prefix != venv:
        sys.exit(f"must run from the campaign venv ({venv}), but sys.prefix is {prefix}\n"
                 f"invoke as: {venv}/bin/mechababs init …")

    # State guard: never clobber add-dataset rows. Reset = delete the ledger first.
    if state.state_path(campaign).is_file():
        sys.exit(f"{state.STATE_FILENAME} already exists — refusing to overwrite.\n"
                 f"To reset, delete it first, then re-run: mechababs configure …")

    pipeline_files = [p.strip() for p in args.pipelines.split(",") if p.strip()]
    if not pipeline_files:
        sys.exit("--pipelines must list at least one pipeline config file")

    pipelines = construct.build(campaign, pipeline_files, args.cluster,
                                str(venv.relative_to(campaign)), limit=args.limit)
    print(f"campaign constructed: pipelines {', '.join(pipelines)}", file=sys.stderr)
    print("Next: mechababs add-dataset <url>; mechababs iterate", file=sys.stderr)
    return 0


STUDY_URL_TEMPLATE = "https://github.com/OpenNeuroStudies/study-{ds_id}"


def default_study_url(ds_id):
    """The OpenNeuroStudies study for a dataset, by convention (``study-<id>``)."""
    return STUDY_URL_TEMPLATE.format(ds_id=ds_id)


def cmd_add_dataset(args):
    """Register a dataset: clone its study into the campaign, append one ledger row.

    The derivative is produced inside a study (cloned from OpenNeuroStudies), so
    add-dataset clones that study now — ``study-<id>`` by convention, or ``--study``
    to override (a non-OpenNeuro study, e.g. an e2e fixture). Only the study
    skeleton is fetched (submodule pointers); sourcedata content is pulled later by
    ``babs init``.

    Derives ``processing_level`` from the cloned study's metadata TSV (has-sessions
    → session) and records it as an INPUT column — iterate reads it, never overwrites
    it, and it is hand-editable. ``--processing-level`` sets it explicitly, bypassing
    the derivation — needed for a study without the metadata TSV (e.g. an e2e
    fixture). On a read failure with no override it is left blank (set it by hand).
    All pipeline columns start empty; no inclusion is generated (selection is
    pipeline-axis, deferred to deploy — see scaffold).
    """
    campaign = _ensure_campaign(args)
    guard.require_clean_pins(campaign)

    ds_id = iterate_mod.dataset_id(args.url)
    study_url = args.study or default_study_url(ds_id)

    # Clone the study into the campaign (registers it as a subdataset). Outside the
    # ledger lock — it's slow and touches no ledger state. A present study dir means
    # this dataset was already added; the ledger dedup below is the authority, but
    # bail early rather than let datalad clone fail on a non-empty target.
    study_dest = campaign / "studies" / f"study-{ds_id}"
    if study_dest.exists():
        sys.exit(f"study already present at {study_dest.relative_to(campaign)} "
                 f"— already added? (reset: remove it and the ledger row)")
    print(f"cloning study {study_url} -> {study_dest.relative_to(campaign)}", file=sys.stderr)
    subprocess.run(
        ["datalad", "clone", "--dataset", str(campaign), study_url,
         str(study_dest.relative_to(campaign))],
        cwd=str(campaign), check=True,
    )

    # processing_level is a dataset property (has-sessions -> session), derived from
    # the CLONED study's metadata TSV and recorded as an INPUT column (iterate reads
    # it, never overwrites; hand-editable). scaffold re-reads the same local TSV for
    # the per-(dataset,pipeline) inclusion — a different axis, same file, no network.
    # --processing-level sets it explicitly (a non-OpenNeuro study without the TSV).
    if args.processing_level:
        processing_level = args.processing_level
        print(f"using --processing-level {processing_level} for {ds_id} "
              f"(bypassing metadata derivation)", file=sys.stderr)
    else:
        try:
            _, processing_level = select.read_study_metadata(study_dest)
        except Exception as e:
            processing_level = ""
            print(f"warning: could not derive processing_level for {ds_id} ({e}); "
                  f"left blank — set it in the ledger before iterate", file=sys.stderr)

    with utils.locked(campaign):
        cols = state.header(campaign)
        rows = state.read_rows(campaign)
        if any(r["dataset_id"] == ds_id for r in rows):
            sys.exit(f"already registered: {ds_id}")
        row = {c: "" for c in cols}
        row["dataset_id"] = ds_id
        row["study_url"] = study_url
        row["processing_level"] = processing_level
        rows.append(row)
        state.write_rows(campaign, cols, rows)
        state.save(campaign,
                   f"add-dataset {ds_id} (study {study_url}, "
                   f"{processing_level or 'level TBD'})")

    print(f"registered {ds_id} (study {study_url}, "
          f"processing_level: {processing_level or 'blank'})", file=sys.stderr)
    return 0


def cmd_iterate(args):
    """One reconciler tick: scaffold each (dataset, pipeline) whose init is empty."""
    campaign = _ensure_campaign(args)
    guard.require_clean_pins(campaign)
    if not args.dry_run:
        iterate_mod.warn_if_no_tmux()
    iterate_mod.run_iterate(campaign, batch=args.batch, dry_run=args.dry_run)
    return 0


def cmd_retire_derivative(args):
    """Move derivative(s) into derivative-attempts/ and reset their ledger cells."""
    campaign = _ensure_campaign(args)
    return retire_mod.run_retire(campaign, args.paths, dry_run=args.dry_run)


def cmd_status(args):
    """Read-only: one row per job across every (dataset, pipeline) cell."""
    campaign = _ensure_campaign(args)
    return status_mod.run_status(
        campaign,
        study=args.study,
        derivative=args.derivative,
        only_failed=args.failed,
        do_refresh=not args.no_refresh,
        output=args.output,
    )


def main():
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"mechababs {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("configure",
                        help="bind an ordered pipeline-set to a cluster (run from the campaign venv)")
    pc.add_argument("--campaign-path", type=Path, default=Path("."),
                    help="the campaign dataset (default: current directory)")
    pc.add_argument("--pipelines", required=True,
                    help="comma-separated pipeline configs (ordered): a path to copy into the "
                         "campaign's pipelines/, or the name of one already there")
    pc.add_argument("--cluster", required=True,
                    help="cluster config: a path to copy into the campaign's clusters/, or the "
                         "name of one already there")
    pc.add_argument("--limit", type=int, default=None,
                    help="cap each dataset's inclusion to the first N eligible subjects "
                         "(default: all)")
    pc.set_defaults(func=cmd_configure)

    pa = sub.add_parser("add-dataset", help="clone a dataset's study, append a ledger row")
    pa.add_argument("url", help="the dataset's upstream URL (its identity)")
    pa.add_argument("--campaign-path", type=Path, default=Path("."),
                    help="the campaign dataset (default: current directory)")
    pa.add_argument("--study", default=None,
                    help="the study to clone (default: OpenNeuroStudies/study-<id> by "
                         "convention); override for a non-OpenNeuro study, e.g. a test fixture")
    pa.add_argument("--processing-level", choices=["subject", "session"], default=None,
                    help="set processing_level explicitly, bypassing OpenNeuroStudies "
                         "derivation (needed for a non-OpenNeuro dataset)")
    pa.set_defaults(func=cmd_add_dataset)

    pi = sub.add_parser("iterate", help="advance pending pipelines one scaffold transition")
    pi.add_argument("--campaign-path", type=Path, default=Path("."),
                    help="the campaign dataset (default: current directory)")
    pi.add_argument("--batch", type=int, default=None,
                    help="cap to N (dataset, pipeline) pairs this tick (default: all)")
    pi.add_argument("--dry-run", action="store_true",
                    help="print the planned commands and change nothing")
    pi.set_defaults(func=cmd_iterate)

    pr = sub.add_parser(
        "retire-derivative",
        help="move a derivative into derivative-attempts/ and reset its ledger cell",
        description=(
            "Move a derivative out of its study into derivative-attempts/"
            "<dataset_id>-<derivative>-attempt-<N> and reset its ledger cell, so the "
            "next iterate re-scaffolds it. Keeps the logs, git history and run records "
            "that say why the cell was redone. NOTE: the retired copy is an ARCHIVE, "
            "not a resumable babs project — babs bakes absolute RIA paths at init, so "
            "after the move its input/output siblings point at the old location and "
            "babs commands (and datalad get/push via those siblings) will not work on "
            "it. Retire a cell you intend to redo from scratch, not one to continue."
        ),
    )
    pr.add_argument("paths", nargs="+", metavar="PATH",
                    help="derivative path(s): studies/study-<id>/derivatives/<name>")
    pr.add_argument("--campaign-path", type=Path, default=Path("."),
                    help="the campaign dataset (default: current directory)")
    pr.add_argument("--dry-run", action="store_true",
                    help="print the planned retirements and change nothing")
    pr.set_defaults(func=cmd_retire_derivative)

    ps = sub.add_parser("status", help="campaign-wide job table (read-only)")
    ps.add_argument("--campaign-path", type=Path, default=Path("."),
                    help="the campaign dataset (default: current directory)")
    ps.add_argument("-o", "--output", choices=["columns", "tsv", "vd"], default="columns",
                    help="aligned table (default), raw TSV to pipe anywhere, or open VisiData")
    ps.add_argument("--study", default=None,
                    help="only this study (ds004044 or study-ds004044)")
    ps.add_argument("--derivative", default=None,
                    help="only this derivative (e.g. MRIQC-24.0.2)")
    ps.add_argument("--failed", action="store_true", help="only jobs that failed")
    ps.add_argument("--no-refresh", action="store_true",
                    help="skip the per-cell `babs status` refresh; read the (possibly stale) cache")
    ps.set_defaults(func=cmd_status)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
