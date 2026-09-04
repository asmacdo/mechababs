"""mechababs — the user CLI (campaign init / add-dataset / iterate / …).

``campaign init`` creates a campaign inside a study — its config, its pinned
environment, and its empty statefile — and is the one verb that runs before that
environment exists, so it may run from anywhere (typically ``uvx --from git+…``).
Every other verb operates on the campaign selected by ``MECHABABS_CAMPAIGN``, from
the study it lives in, and runs from that campaign's own venv. The action verbs
``iterate`` dispatches under ``datalad run`` live in ``mechababs-inner``.
"""

import argparse
import subprocess
import sys
from pathlib import Path

from mechababs import __version__, campaign_init
from mechababs import add_dataset as add_dataset_mod
from mechababs import campaign as campaign_mod
from mechababs import iterate as iterate_mod
from mechababs import jobs as jobs_mod
from mechababs import retire as retire_mod
from mechababs import status as status_mod
from mechababs import study as study_mod
from mechababs import update_env as update_env_mod
from mechababs import validate as validate_mod


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
    campaigns, each with its own label, configs and lock.
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
    member = "--study <member|url> " if args.superstudy else ""
    print(
        f"  mechababs add-dataset {member}--sourcedata sourcedata/<id>", file=sys.stderr
    )
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
    """One iterate: advance each cell of the selected campaign by at most one transition.

    Runs from the campaign root — the study — like every operating verb; which
    campaign is the env var's answer, not a flag's. The clean check raises rather
    than exits (it is a library guard the verbs share), so it is turned into a plain
    message here: its text is already the explanation, and a traceback would bury it.

    A mechababs inner command that fails is the same case: its own output is
    already on stderr below the `+ <command>` echo, so what is left to say is that
    iterate stopped there, and that everything advanced before it stands recorded.
    """
    try:
        iterate_mod.run_iterate(
            ".",
            batch=args.batch,
            app=args.app,
            study=args.study,
            dry_run=args.dry_run,
        )
    except RuntimeError as e:
        sys.exit(str(e))
    except subprocess.CalledProcessError as e:
        sys.exit(
            f"mechababs iterate stopped: `{' '.join(map(str, e.cmd))}` exited "
            f"{e.returncode} (its output is above).\n"
            "Every cell advanced before it is recorded; fix the cause and run "
            "iterate again."
        )
    return 0


def cmd_retire_derivative(args):
    """Take one derivative out of its study and reset the cell that made it.

    Runs from the campaign root — the study, or the superstudy — like every operating
    verb. Where the derivative goes is a required choice, not a default: `--path`
    keeps its evidence somewhere outside the study, `--remove` throws it away.
    """
    return retire_mod.run_retire(
        args.derivative, dest=args.path, remove=args.remove, root="."
    )


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
    return status_mod.run_status(".", study=args.study, app=args.app)


def cmd_jobs(args):
    """Read-only: one row per job, with the log path for each."""
    return jobs_mod.run_jobs(
        ".",
        study=args.study,
        app=args.app,
        failed=args.failed,
        refresh_first=args.refresh,
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
            "Create a campaign inside an existing study: copy "
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
            "Advance the selected campaign's cells by at most one transition each. "
            "Each cell advances by AT MOST ONE transition, routed on the statefile's "
            "columns: not started -> scaffold; scaffolded and not merged -> what the "
            "live `babs status` counts say (submit / wait / merge / flag a failure); "
            "merged -> skipped. A cell waiting on an unmerged producer is noted and "
            "passed over, not blocked on, and a cell whose jobs failed is flagged "
            "rather than merged. Nothing is remembered between runs: every iterate "
            "re-reads ground truth, so run it again and again until the campaign is "
            "done."
        ),
    )
    pi.add_argument(
        "--batch",
        type=int,
        default=None,
        help="advance at most N cells (default: all). A cell that is "
        "already done, waiting, or still running does not count against it.",
    )
    pi.add_argument(
        "--app",
        default=None,
        metavar="STEM",
        help="only this app config's cells, by its filename stem (e.g. MRIQC-24.0.2)",
    )
    pi.add_argument(
        "--study",
        default=None,
        metavar="MEMBER",
        help="at a superstudy, advance only this member (composable with "
        "--app). Where you stand gives the level; this narrows within it.",
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
        help="take a derivative out of its study and reset the cell that made it",
        description=(
            "Clear a cell that has to be redone. `babs init` refuses an existing "
            "path, so the derivative has to leave before the next iterate can "
            "re-scaffold — and where it goes is a required choice, because the two "
            "answers are not interchangeable. `--path DEST` keeps its evidence (the "
            "logs, the git history, the run records that say WHY the cell was "
            "redone) at DEST/<study>-<derivative>-attempt-<N>; DEST must be outside "
            "the study, and outside the whole superstudy when the campaign is "
            "operated at one, since a study is a published object and a retired "
            "attempt inside it would travel with it. `--remove` deletes the "
            "derivative outright, for a cell whose evidence is worth nothing. Either "
            "way the cell is reset in the same transition, so there is no window "
            "where the derivative is gone but the reconciler still routes it as "
            "in-progress. NOTE: what --path produces is an ARCHIVE, not a resumable "
            "babs project — babs bakes absolute RIA paths at init, so after the move "
            "its input/output siblings name the old location and babs commands (and "
            "datalad get/push through those siblings) will not work on it. Retire a "
            "cell you intend to redo from scratch, not one to continue."
        ),
    )
    pr.add_argument(
        "derivative",
        metavar="PATH",
        help="the derivative, campaign-relative or absolute: derivatives/<name> at "
        "a study, <member>/derivatives/<name> at a superstudy",
    )
    # Required and mutually exclusive: keeping the evidence and throwing it away are
    # different decisions, and neither is safe to make on the user's behalf.
    where = pr.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "--path",
        default=None,
        metavar="DEST",
        help="archive the derivative under DEST (created if absent), which must be "
        "outside the study — and outside the superstudy, at one",
    )
    where.add_argument(
        "--remove",
        action="store_true",
        help="delete the derivative instead of archiving it, discarding its logs "
        "and history with it",
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
            "Render the selected campaign's cells, one row each — the statefile as it "
            "is, plus the part it deliberately does not store: for a cell whose jobs "
            "are running, the live `babs status` counts. At a superstudy the rows "
            "span every member, computed from their shards at the moment you look, "
            "with a column saying which members are on disk. Read-only, and it takes "
            "no lock, so it can be run while an iterate is in progress. The table goes to "
            "stdout and the summary to stderr, so it stays pipeable."
        ),
    )
    ps.add_argument(
        "--study",
        default=None,
        metavar="MEMBER",
        help="at a superstudy, only this member's cells. Matched against the "
        "campaign's catalog, so a directory that was never selected in is an "
        "error rather than an empty table.",
    )
    ps.add_argument(
        "--app",
        default=None,
        metavar="STEM",
        help="only this app config's cells, by its filename stem (e.g. "
        "MRIQC-24.0.2). Checked against the campaign's declared apps, so a typo "
        "is refused even when nothing is installed to compare against.",
    )
    ps.set_defaults(func=cmd_status)

    pj = sub.add_parser(
        "jobs",
        help="one row per job of the selected campaign (read-only)",
        description=(
            "The drill-down under `status`'s cells: every job babs is tracking, "
            "tagged with the study, source dataset and app it belongs to, and the "
            "path to its log. A cell with nothing submitted has no job_status.csv "
            "and is left out. Read-only, and it takes no lock."
        ),
    )
    pj.add_argument(
        "--study",
        default=None,
        metavar="MEMBER",
        help="at a superstudy, only this member's jobs",
    )
    pj.add_argument(
        "--app",
        default=None,
        metavar="STEM",
        help="only this app config's jobs, by its filename stem",
    )
    pj.add_argument(
        "--failed",
        action="store_true",
        help="only jobs babs marks failed (ended without results)",
    )
    pj.add_argument(
        "--no-refresh",
        dest="refresh",
        action="store_false",
        help="read babs's job_status.csv as it stands instead of recomputing it "
        "from the scheduler first. Faster, and an explicit choice: a stale row can "
        "show a resubmitted job as failed.",
    )
    pj.set_defaults(func=cmd_jobs)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
