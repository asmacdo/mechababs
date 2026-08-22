"""mechababs — the operate-side CLI (configure / add-dataset / iterate / …).

Runs inside a campaign's venv (built by bootstrap.sh). ``configure`` binds an
ordered pipeline-set to a cluster (the mechababs config + the ledger) from inside
that venv; the other subcommands mutate or advance the state-file ledger. The
environment half of the bootstrap — datalad dataset, vendored code pins, venv —
is bootstrap.sh's job.
"""

import argparse
import sys
from pathlib import Path

from mechababs import __version__, campaign_init, construct, guard, state
from mechababs import add_dataset as add_dataset_mod
from mechababs import campaign as campaign_mod
from mechababs import iterate as iterate_mod
from mechababs import retire as retire_mod
from mechababs import status as status_mod
from mechababs import study as study_mod
from mechababs import validate as validate_mod


def _ensure_campaign(args):
    """Resolve --campaign-path and confirm it is a campaign (i.e. has a ledger)."""
    campaign = args.campaign_path.resolve()
    if not state.state_path(campaign).is_file():
        sys.exit(f"not a campaign (no {state.STATE_FILENAME}): {campaign}")
    return campaign


def _ensure_campaign_skeleton(args):
    """Resolve --campaign-path and confirm it is the campaign ENVIRONMENT bootstrap built.

    A datalad dataset with both code pins registered — deliberately NOT the ledger, which
    is what `_ensure_campaign` checks. Both commands that use this run *before* a ledger
    exists: `configure` is what writes it, and `test-cluster` validates a cluster before
    you commit real data to it, which is the whole point of validating.
    """
    campaign = args.campaign_path.resolve()
    if not (campaign / ".datalad").is_dir():
        sys.exit(f"not a datalad dataset: {campaign}")
    for sub in ("code/mechababs", "code/babs"):
        if not (campaign / sub).is_dir():
            sys.exit(f"not a campaign skeleton (missing {sub}): {campaign}")
    return campaign


def _require_campaign_venv(campaign):
    """Refuse unless THIS process is the campaign venv's python.

    The guard that kills the wrong-babs bug: the campaign's `.venv` is where the
    pinned babs + mechababs live, so an ambient install running instead would
    scaffold (or validate) with tools the campaign does not record.
    """
    venv = (campaign / ".venv").resolve()
    prefix = Path(sys.prefix).resolve()
    if prefix != venv:
        sys.exit(
            f"must run from the campaign venv ({venv}), but sys.prefix is {prefix}\n"
            f"invoke as: {venv}/bin/mechababs …"
        )
    return venv


def cmd_campaign_init(args):
    """Create a campaign in the study you are standing in.

    The one command that runs before a campaign environment exists — so it is the
    one that may run from anywhere (typically ``uvx --from git+…``), and the one
    that does not take the env-match guard. It creates the environment the guard
    will check from here on.

    The study is the current directory, not a flag: study-first commands operate on
    where you are, and this one has no ledger or config to point elsewhere with.
    """
    study = study_mod.require_study_root(".")
    # `--apps a.yaml,b.yaml` (as the quickstart shows) and a repeated `--apps` both
    # work, and compose — the bundle is ordered as written either way.
    apps = [
        app.strip() for group in args.apps for app in group.split(",") if app.strip()
    ]
    campaign = campaign_init.init(
        study,
        args.label,
        apps,
        args.cluster,
        limit=args.limit,
        babs_spec=args.babs,
        mechababs_spec=args.mechababs,
    )
    rel = campaign.relative_to(study)
    print(f"\ncampaign {args.label!r} created at {rel}", file=sys.stderr)
    print(
        "Next, select it and activate its environment, then add data:", file=sys.stderr
    )
    print(f"  source {rel}/{campaign_mod.ENV_FILENAME}", file=sys.stderr)
    print("  mechababs add-dataset --sourcedata sourcedata/<id>", file=sys.stderr)
    return 0


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
    # Look like a campaign skeleton bootstrap.sh built?
    campaign = _ensure_campaign_skeleton(args)

    # Provenance guard: the code pins must match what the campaign records.
    guard.require_clean_pins(campaign)

    # The PATH guard, the whole point: are we the campaign venv's python? If not, an
    # ambient mechababs is running and would scaffold with the wrong (unpinned) babs.
    venv = _require_campaign_venv(campaign)

    # State guard: never clobber add-dataset rows. Reset = delete the ledger first.
    if state.state_path(campaign).is_file():
        sys.exit(
            f"{state.STATE_FILENAME} already exists — refusing to overwrite.\n"
            f"To reset, delete it first, then re-run: mechababs configure …"
        )

    pipeline_files = [p.strip() for p in args.pipelines.split(",") if p.strip()]
    if not pipeline_files:
        sys.exit("--pipelines must list at least one pipeline config file")

    pipelines = construct.build(
        campaign,
        pipeline_files,
        args.cluster,
        str(venv.relative_to(campaign)),
        limit=args.limit,
    )
    print(f"campaign constructed: pipelines {', '.join(pipelines)}", file=sys.stderr)
    print("Next: mechababs add-dataset <url>; mechababs iterate", file=sys.stderr)
    return 0


def cmd_add_dataset(args):
    """Select a source dataset already in a study into the selected campaign.

    Sniff + add-state-entry: read the study's per-subject metadata for this source
    dataset to fill the cell's identity columns, then write one cell per app in the
    campaign's bundle. No data is installed, and no inclusion is generated (that is
    scaffold's, where the eligibility rule applies).

    Runs from the campaign root — the study — like every operating verb;
    ``--sourcedata`` is a path inside it.
    """
    added = add_dataset_mod.add(args.sourcedata)
    cell = added[0]  # identity is the same across a dataset's cells
    size = f"{cell['n_subjects']} subjects"
    if cell["n_sessions"]:
        size += f", {cell['n_sessions']} sessions"
    print(
        f"selected {cell['source_dataset']} ({cell['processing_level']}-level, "
        f"{size}) — {len(added)} cell(s): "
        f"{', '.join(Path(row['app_config']).stem for row in added)}",
        file=sys.stderr,
    )
    print("Next: mechababs iterate", file=sys.stderr)
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


def cmd_test_cluster(args):
    """Validate a cluster config end to end, using this campaign's pinned tools.

    Runs from the campaign venv for the same reason `configure` does: the pinned babs
    is what makes the result mean anything. The scenario builds its own throwaway
    campaign to work in — it configures and retires derivatives, so it must not touch
    this one.

    Takes the campaign SKELETON, not a configured campaign: validating a cluster before
    committing real data to it is the point, so this has to work on a campaign that has
    only been bootstrapped (no ledger yet).
    """
    campaign = _ensure_campaign_skeleton(args)
    guard.require_clean_pins(campaign)
    _require_campaign_venv(campaign)
    # argparse.REMAINDER keeps the `--` separator in the list; pytest does not need it.
    extra = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
    return validate_mod.run_test_cluster(
        campaign,
        args.cluster,
        extra_args=extra,
        workdir=args.workdir,
    )


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

    pcamp = sub.add_parser(
        "campaign", help="create a campaign in this study, or rebuild its environment"
    )
    camp_sub = pcamp.add_subparsers(dest="campaign_cmd", required=True)
    pci = camp_sub.add_parser(
        "init",
        help="create a campaign in the study you are standing in",
        description=(
            "Create a campaign — one config epoch — inside an existing study: copy "
            "your app + cluster configs into .mechababs/campaigns/<label>/, pin "
            "mechababs + babs into a uv.lock, build the campaign's venv from that "
            "lock, and write the empty statefile. Which source datasets the campaign "
            "acts on is a separate, explicit step (`add-dataset`). This is the one "
            "command that runs before the campaign environment exists, so it can be "
            "run ephemerally: `uvx --from git+https://github.com/con/mechababs@<ref> "
            "mechababs campaign init …`."
        ),
    )
    pci.add_argument(
        "label",
        help="the campaign's identity (its directory name, and "
        "what MECHABABS_CAMPAIGN selects)",
    )
    pci.add_argument(
        "--apps",
        action="append",
        required=True,
        metavar="PATH|URL[,…]",
        help="BIDS-App configs, ordered: paths or URLs, copied into the "
        "campaign. Comma-separated, and repeatable.",
    )
    pci.add_argument(
        "--cluster",
        required=True,
        metavar="PATH|URL",
        help="cluster config: a path or URL, copied into the campaign",
    )
    pci.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap each source dataset's inclusion to the first N eligible "
        "subjects (default: all)",
    )
    pci.add_argument(
        "--babs",
        default=None,
        metavar="URL@REF",
        help="pin babs to a git checkout instead of the default, which is "
        "the latest babs release from PyPI, frozen to an exact version "
        "by the lock. URL is anything git clones, a local checkout "
        "included — which is how a PR branch gets run through a campaign.",
    )
    pci.add_argument(
        "--mechababs",
        default=None,
        metavar="URL@REF",
        help="the mechababs to pin (default: whichever mechababs is "
        "running this command, pinned by its resolved commit)",
    )
    pci.set_defaults(func=cmd_campaign_init)

    pc = sub.add_parser(
        "configure",
        help="bind an ordered pipeline-set to a cluster (run from the campaign venv)",
    )
    pc.add_argument(
        "--campaign-path",
        type=Path,
        default=Path("."),
        help="the campaign dataset (default: current directory)",
    )
    pc.add_argument(
        "--pipelines",
        required=True,
        help="comma-separated pipeline configs (ordered): a path to copy into the "
        "campaign's pipelines/, or the name of one already there",
    )
    pc.add_argument(
        "--cluster",
        required=True,
        help="cluster config: a path to copy into the campaign's clusters/, or the "
        "name of one already there",
    )
    pc.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap each dataset's inclusion to the first N eligible subjects "
        "(default: all)",
    )
    pc.set_defaults(func=cmd_configure)

    pa = sub.add_parser(
        "add-dataset",
        help="select a source dataset already in a study into this campaign",
        description=(
            "Select which data the campaign acts on. Run from the study root (the "
            "campaign root); --sourcedata names a source dataset ALREADY in the "
            "study, and one cell per app in the campaign's bundle is written into "
            "the study's statefile. add-dataset does not "
            "install data and does not generate a subject inclusion (that happens "
            "at scaffold, where the app's eligibility rule applies)."
        ),
    )
    pa.add_argument(
        "--sourcedata",
        metavar="PATH",
        required=True,
        help="a source dataset already in this study (e.g. sourcedata/ds000001)",
    )
    pa.set_defaults(func=cmd_add_dataset)

    pi = sub.add_parser(
        "iterate", help="advance pending pipelines one scaffold transition"
    )
    pi.add_argument(
        "--campaign-path",
        type=Path,
        default=Path("."),
        help="the campaign dataset (default: current directory)",
    )
    pi.add_argument(
        "--batch",
        type=int,
        default=None,
        help="cap to N (dataset, pipeline) pairs this tick (default: all)",
    )
    pi.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned commands and change nothing",
    )
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
    pr.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="derivative path(s): studies/study-<id>/derivatives/<name>",
    )
    pr.add_argument(
        "--campaign-path",
        type=Path,
        default=Path("."),
        help="the campaign dataset (default: current directory)",
    )
    pr.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned retirements and change nothing",
    )
    pr.set_defaults(func=cmd_retire_derivative)

    pt = sub.add_parser(
        "test-cluster",
        help="validate a cluster config end to end, using this campaign's pinned tools",
        description=(
            "Run the e2e scenario against a cluster config: configure -> add-dataset -> "
            "iterate (scaffold -> submit -> merge), asserting a real derivative landed. "
            "A stronger check than `babs check-setup`, because it proves the config "
            "actually produces output on this scheduler. Uses this campaign's pinned "
            "babs + mechababs and its venv, so no repo checkout or env-var setup is "
            "needed. NOTE: the scenario builds its OWN throwaway campaign to work in "
            "(it configures and retires derivatives); this campaign supplies the "
            "environment, not the workspace."
        ),
    )
    pt.add_argument(
        "--cluster",
        required=True,
        help="cluster config to validate: a path, or the name of one in the "
        "campaign's clusters/",
    )
    pt.add_argument(
        "--campaign-path",
        type=Path,
        default=Path("."),
        help="the campaign dataset (default: current directory)",
    )
    pt.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="where to build the scenario's campaign (default: beside this "
        "campaign, so the container shim resolves as its sibling)",
    )
    # Flag-looking pytest args have to be fenced off from this parser, so they go
    # after a literal `--` (the usual convention: `uv run --`, `npm run x --`).
    pt.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        metavar="-- PYTEST_ARGS",
        help="args after a literal `--` pass through to pytest "
        "(e.g. `-- -k test_full_run`)",
    )
    pt.set_defaults(func=cmd_test_cluster)

    ps = sub.add_parser("status", help="campaign-wide job table (read-only)")
    ps.add_argument(
        "--campaign-path",
        type=Path,
        default=Path("."),
        help="the campaign dataset (default: current directory)",
    )
    ps.add_argument(
        "-o",
        "--output",
        choices=["columns", "tsv", "vd"],
        default="columns",
        help="aligned table (default), raw TSV to pipe anywhere, or open VisiData",
    )
    ps.add_argument(
        "--study", default=None, help="only this study (ds004044 or study-ds004044)"
    )
    ps.add_argument(
        "--derivative", default=None, help="only this derivative (e.g. MRIQC-24.0.2)"
    )
    ps.add_argument("--failed", action="store_true", help="only jobs that failed")
    ps.add_argument(
        "--no-refresh",
        action="store_true",
        help="skip the per-cell `babs status` refresh; read the (possibly stale) cache",
    )
    ps.set_defaults(func=cmd_status)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
