"""mechababs — the user CLI (campaign init / add-dataset / iterate / …).

``campaign init`` creates a campaign inside a study — its config, its pinned
environment, and its empty statefile — and is the one verb that runs before that
environment exists, so it may run from anywhere (typically ``uvx --from git+…``).
Every other verb operates on the campaign selected by ``MECHABABS_CAMPAIGN``, from
the study it lives in, and runs from that campaign's own venv. The action verbs
``iterate`` dispatches under ``datalad run`` live in ``mechababs-inner``.
"""

import argparse
import sys
from pathlib import Path

from mechababs import __version__, campaign_init, state
from mechababs import add_dataset as add_dataset_mod
from mechababs import campaign as campaign_mod
from mechababs import iterate as iterate_mod
from mechababs import retire as retire_mod
from mechababs import status as status_mod
from mechababs import study as study_mod
from mechababs import update_env as update_env_mod
from mechababs import validate as validate_mod


def _ensure_campaign(args):
    """Resolve --campaign-path and confirm it is a campaign (i.e. has a ledger)."""
    campaign = args.campaign_path.resolve()
    if not state.state_path(campaign).is_file():
        sys.exit(f"not a campaign (no {state.STATE_FILENAME}): {campaign}")
    return campaign


def cmd_campaign_init(args):
    """Create a campaign in the study you are standing in.

    The one command that runs before a campaign environment exists — so it is the
    one that may run from anywhere (typically ``uvx --from git+…``), and the one
    that does not take the env-match guard. It creates the environment the guard
    will check from here on.

    Alone among the verbs it names its target rather than taking the cwd: ``-d``
    mirrors datalad's, and ``--superstudy NAME`` names a superstudy to create or
    adopt. Every other verb runs from the root of the dataset that owns the
    campaign, which is what makes "operate a campaign only from the level it was
    configured" checkable rather than conventional.

    The superstudy and the campaign are named separately because they are not the
    same thing and do not share a lifetime: one superstudy accumulates many
    campaigns, each its own label and config epoch.
    """
    if args.superstudy:
        root = campaign_init.create_superstudy(args.superstudy)
    else:
        root = study_mod.require_study_root(args.dataset or ".")
    study = root
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
        superstudy=bool(args.superstudy),
    )
    rel = campaign.relative_to(study)
    print(f"\ncampaign {args.label!r} created at {rel}", file=sys.stderr)
    print(
        "Next, select it and activate its environment, then add data:", file=sys.stderr
    )
    print(f"  source {rel}/{campaign_mod.ENV_FILENAME}", file=sys.stderr)
    print("  mechababs add-dataset --sourcedata sourcedata/<id>", file=sys.stderr)
    return 0


def cmd_campaign_update_env(args):
    """Converge the selected campaign's environment on its declaration.

    The second command exempt from the env-match guard, and for the mirror-image
    reason to ``campaign init``'s: init runs before the environment exists, this runs
    when it is absent or wrong. Both still take the configured-level context, so a
    member is reached with ``--study`` from the superstudy rather than by standing in
    it.
    """
    return update_env_mod.run_update_env(".", upgrade=args.upgrade, member=args.study)


def cmd_add_dataset(args):
    """Select a source dataset already in a study into the selected campaign.

    Sniff + add-state-entry: read the study's per-subject metadata for this source
    dataset to fill the cell's identity columns, then write one cell per app in the
    campaign's bundle. No data is installed, and no inclusion is generated (that is
    scaffold's, where the eligibility rule applies).

    Runs from the campaign root — the study — like every operating verb;
    ``--sourcedata`` is a path inside it.
    """
    added = add_dataset_mod.add(args.sourcedata, args.study)
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
    """One reconciler tick: advance each cell of the selected campaign by one step.

    Runs from the campaign root — the study — like every operating verb; which
    campaign is the env var's answer, not a flag's. The clean check raises rather
    than exits (it is a library guard the verbs share), so it is turned into a plain
    message here: its text is already the explanation, and a traceback would bury it.
    """
    try:
        iterate_mod.run_iterate(
            ".",
            batch=args.batch,
            derivative=args.derivative,
            study=args.study,
            dry_run=args.dry_run,
        )
    except RuntimeError as e:
        sys.exit(str(e))
    return 0


def cmd_retire_derivative(args):
    """Move derivative(s) into derivative-attempts/ and reset their ledger cells."""
    campaign = _ensure_campaign(args)
    return retire_mod.run_retire(campaign, args.paths, dry_run=args.dry_run)


def cmd_test_cluster(args):
    """Validate a cluster config end to end, in a throwaway study.

    Runs from anywhere — there is nothing to stand in. A campaign lives inside a
    study, so the scenario builds a fixture study on the scratch path and runs the
    real spine in it; real studies are never touched.

    With no `--mechababs`, the fixture campaign pins whichever mechababs is running
    this command, so what gets validated is the code you invoked. babs is a dependency
    of the *generated campaign*, not of mechababs, so it cannot mirror the caller —
    the fixture campaign gets what a user's campaign would get, unless `--babs` says
    otherwise.
    """
    # argparse.REMAINDER keeps the `--` separator in the list; pytest does not need it.
    extra = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
    return validate_mod.run_test_cluster(
        args.cluster,
        args.scratch_path,
        extra_args=extra,
        mechababs=args.mechababs,
        babs=args.babs,
    )


def cmd_status(args):
    """Read-only: one row per cell, with live job counts for the running ones."""
    return status_mod.run_status(".")


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
    # Both name the target, so argparse refuses them together rather than the
    # command choosing a winner. -d is the study side and mirrors datalad's;
    # --superstudy is the superstudy side and may name one that does not exist yet.
    target = pci.add_mutually_exclusive_group()
    target.add_argument(
        "-d",
        "--dataset",
        default=None,
        metavar="PATH",
        help="the study to create the campaign in, named the way datalad's -d "
        "is (default: the current directory)",
    )
    target.add_argument(
        "--superstudy",
        default=None,
        metavar="NAME",
        help="create the campaign at a superstudy of this name, creating the "
        "superstudy if it is not there yet and adopting it if it is. A "
        "superstudy holds many campaigns over time, so it is named separately "
        "from the campaign label.",
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

    pue = camp_sub.add_parser(
        "update-env",
        help="converge this campaign's environment on its declaration",
        description=(
            "Re-resolve the campaign's pyproject.toml into its uv.lock, install "
            "exactly that into the campaign venv, and commit both if either moved. "
            "What it does follows from the declaration: untouched, the lock does not "
            "move and the venv is simply rebuilt from it (a fresh clone, a wiped "
            "site, a historical checkout during rerun-reproduction); edited, the "
            "change re-resolves and installs — the deliberate mid-campaign bump. To "
            "bump, edit .mechababs/campaigns/<label>/pyproject.toml by hand (the "
            "pins are `rev` lines under [tool.uv.sources]) and run this. Committed "
            "as a plain save rather than a `datalad run`: `uv lock` resolves against "
            "the live world, so recording it as re-executable would be a false "
            "promise — the lock is the reproducible artifact."
        ),
    )
    pue.add_argument(
        "--upgrade",
        action="append",
        default=[],
        metavar="PKG",
        help="re-resolve PKG to the newest thing its declaration allows, without "
        "editing the declaration: the case with nothing to hand-edit, a pin "
        "tracking a branch whose tip moved. Repeatable. Touches only the lock.",
    )
    pue.add_argument(
        "--study",
        default=None,
        metavar="MEMBER",
        help="at a superstudy, also copy the resulting lock into this member's "
        "footprint — the acknowledgment that its remaining work moves onto the "
        "new environment. The lock only; the member's configs are never touched.",
    )
    pue.set_defaults(func=cmd_campaign_update_env)

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
        "--study",
        default=None,
        metavar="PATH|URL",
        help="at a superstudy, the member holding the source dataset: a member "
        "already there, or a URL to clone one in. --sourcedata is then relative "
        "to that member. Not for a study-configured campaign, which has no "
        "members.",
    )
    pa.add_argument(
        "--sourcedata",
        metavar="PATH",
        required=True,
        help="a source dataset already in this study (e.g. sourcedata/ds000001)",
    )
    pa.set_defaults(func=cmd_add_dataset)

    pi = sub.add_parser(
        "iterate",
        help="advance the selected campaign's cells by one transition each",
        description=(
            "One reconciler tick over this study's cells for the selected campaign. "
            "Each cell advances by AT MOST ONE transition, routed on the statefile's "
            "columns: not started -> scaffold; scaffolded and not merged -> what the "
            "live `babs status` counts say (submit / wait / merge / flag a failure); "
            "merged -> skipped. A cell waiting on an unmerged producer is noted and "
            "passed over, not blocked on, and a cell whose jobs failed is flagged "
            "rather than merged. Nothing is remembered between ticks: every tick "
            "re-reads ground truth, so run it again and again until the campaign is "
            "done."
        ),
    )
    pi.add_argument(
        "--batch",
        type=int,
        default=None,
        help="advance at most N cells this tick (default: all). A cell that is "
        "already done, waiting, or still running does not count against it.",
    )
    pi.add_argument(
        "--derivative",
        default=None,
        metavar="STEM",
        help="only this app config's cells, by its filename stem (e.g. MRIQC-24.0.2)",
    )
    pi.add_argument(
        "--study",
        default=None,
        metavar="MEMBER",
        help="at a superstudy, advance only this member (composable with "
        "--derivative). Where you stand gives the level; this narrows within it.",
    )
    pi.add_argument(
        "--dry-run",
        action="store_true",
        help="route every cell for real and print the transitions it would "
        "dispatch, without dispatching them",
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
        help="validate a cluster config end to end, in a throwaway study",
        description=(
            "Run the e2e scenario against a cluster config: campaign init -> "
            "add-dataset -> iterate (scaffold -> submit -> merge), asserting a real "
            "derivative landed. A stronger check than `babs check-setup`, because it "
            "proves the config actually produces output on this scheduler. The "
            "scenario builds its OWN fixture study on the scratch path and works "
            "there; real studies are never touched. With no --mechababs it recreates "
            "the environment it was called from — the fixture campaign pins whichever "
            "mechababs is running this command. It runs the packaged suite with this "
            "interpreter's pytest, so install mechababs with its `test` extra: "
            "uvx --from 'git+https://github.com/con/mechababs@<ref>#egg=mechababs[test]' "
            "mechababs test-cluster --cluster <site.yaml> --scratch-path <scratch>"
        ),
    )
    pt.add_argument(
        "--cluster",
        required=True,
        metavar="PATH",
        help="the cluster config to validate, by path (configs are user-provided, "
        "never a name mechababs looks up)",
    )
    pt.add_argument(
        "--scratch-path",
        required=True,
        metavar="DIR",
        help="scratch dir the scenario works in: the fixture studies, the container "
        "dataset they resolve as their sibling, and the caches. Put it on fast "
        "cluster scratch — never home or /tmp.",
    )
    pt.add_argument(
        "--babs",
        default=None,
        metavar="URL@REF",
        help="pin babs to a git checkout instead of the default, which is what a "
        "user's own campaign gets: the latest release, frozen by the campaign's lock",
    )
    pt.add_argument(
        "--mechababs",
        default=None,
        metavar="URL@REF",
        help="the mechababs the fixture campaign pins (default: whichever mechababs "
        "is running this command, pinned by its resolved commit)",
    )
    # Flag-looking pytest args have to be fenced off from this parser, so they go
    # after a literal `--` (the usual convention: `uv run --`, `npm run x --`).
    pt.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        metavar="-- PYTEST_ARGS",
        help="args after a literal `--` pass through to pytest "
        "(e.g. `-- -k test_spine`)",
    )
    pt.set_defaults(func=cmd_test_cluster)

    ps = sub.add_parser(
        "status",
        help="one row per cell of the selected campaign (read-only)",
        description=(
            "Render this study's cells for the selected campaign, one row each — the "
            "statefile as it is, plus the part it deliberately does not store: for a "
            "cell whose jobs are running, the live `babs status` counts. Read-only, "
            "and it takes no lock, so it can be run while a tick is in progress."
        ),
    )
    ps.set_defaults(func=cmd_status)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
