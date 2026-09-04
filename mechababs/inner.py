"""mechababs-inner — the action verbs the reconciler dispatches. Not a user CLI.

`mechababs` is what a person runs; `mechababs-inner` is what the reconciler
dispatches — under a `datalad run` for the verbs that change the study
(`scaffold`, `merge`), plainly for the one that does not (`submit`). The split
exists so the two CLIs can have different manners:

- **self-labeling.** Seeing `mechababs-inner scaffold …` in a study's history says
  unambiguously "a machine-dispatched provenance step", not "someone typed this".
- **self-guarding.** Each verb refuses a cell that is not in the state it advances
  from. A bare `datalad rerun` onto current HEAD re-executes the recorded command
  against a cell that has since been scaffolded, and the desired outcome there is
  a loud failure, not a second derivative.
- **no configured-level check, and no location check.** Both live on the outer
  commands, deliberately: user-driven advancing is gated, while reproducing a
  recorded run is not, and *which directory* a venv sits in is a selection question
  that selection already answered.

What an inner verb does check is that the running venv is what the **study it is
standing in** committed as its lock (`require_study_lock_match`, which says why
that one check serves reproduction, replay and member drift alike).

The campaign is a required flag rather than the `MECHABABS_CAMPAIGN` env var, so a
recorded command names what it operated on instead of inheriting it.
"""

import argparse
import sys

from mechababs import __version__
from mechababs import campaign as campaign_mod
from mechababs import merge as merge_mod
from mechababs import scaffold as scaffold_mod
from mechababs import study as study_mod
from mechababs import submit as submit_mod


def _require_context(args):
    """The three preconditions every verb shares: a study, its shard, its env.

    The env one is the study-local lock check, NOT the outer guard: an inner verb
    may legitimately run in a venv the operator built themselves, anywhere the study
    was cloned to, so long as that venv is what the study's own lock describes.
    """
    study = study_mod.require_study_root(".")
    campaign_mod.require_statefile(study, args.campaign)
    campaign_mod.require_study_lock_match(study, args.campaign)
    return study


def cmd_scaffold(args):
    """Advance one cell from "not started" to "initialized" (see scaffold.py)."""
    study = _require_context(args)
    project = scaffold_mod.scaffold(study, args.campaign, args.source_dataset, args.app)
    print(
        f"scaffolded {args.source_dataset} / "
        f"{scaffold_mod.app_stem(args.app)} -> {project}",
        file=sys.stderr,
    )
    return 0


def cmd_submit(args):
    """Deploy one active cell's outstanding jobs (see submit.py)."""
    study = _require_context(args)
    project = submit_mod.submit(study, args.campaign, args.source_dataset, args.app)
    print(
        f"submitted {args.source_dataset} / "
        f"{scaffold_mod.app_stem(args.app)} ({project})",
        file=sys.stderr,
    )
    return 0


def cmd_merge(args):
    """Consolidate one finished cell's results and record it done (see merge.py)."""
    study = _require_context(args)
    project = merge_mod.merge(study, args.campaign, args.source_dataset, args.app)
    print(
        f"merged {args.source_dataset} / "
        f"{scaffold_mod.app_stem(args.app)} -> {project}",
        file=sys.stderr,
    )
    return 0


def _cell_verb(sub, verb, func, *, summary, description):
    """Add a subparser for a verb that names one cell.

    Every action verb takes the same three: which campaign, and the two halves of
    the cell's identity. Written once so a new verb cannot drift from the others —
    the dispatcher builds one argv shape for all of them.
    """
    p = sub.add_parser(verb, help=summary, description=description)
    p.add_argument(
        "--campaign",
        required=True,
        metavar="LABEL",
        help="the campaign whose statefile holds the cell. A flag, not "
        "the env var: a recorded command names what it ran on.",
    )
    p.add_argument(
        "--source-dataset",
        required=True,
        metavar="PATH",
        help="the cell's source dataset, study-relative (e.g. sourcedata/ds000001)",
    )
    p.add_argument(
        "--app",
        required=True,
        metavar="PATH",
        help="the cell's app config, campaign-relative "
        "(e.g. bids-app-configs/MRIQC-24.0.2.yaml)",
    )
    p.set_defaults(func=func)
    return p


def main():
    p = argparse.ArgumentParser(
        prog="mechababs-inner",
        description=__doc__.split("\n\n")[0],
        epilog="Dispatched by `mechababs iterate`; not a command to run by hand.",
    )
    p.add_argument("--version", action="version", version=f"mechababs {__version__}")
    sub = p.add_subparsers(dest="verb", required=True)

    _cell_verb(
        sub,
        "scaffold",
        cmd_scaffold,
        summary="init one cell's derivative and record it",
        description=(
            "Generate the cell's subject inclusion, compose the babs config from "
            "the campaign's app x cluster x source axes, `babs init` the "
            "derivative into the study's derivatives/, pin the requested subject "
            "list, and record the derivative's path in the cell's `babs` column. "
            "Refuses a cell that is already scaffolded, is not in the statefile, "
            "or is waiting on an unmerged producer."
        ),
    )
    _cell_verb(
        sub,
        "submit",
        cmd_submit,
        summary="put one cell's outstanding jobs on the queue",
        description=(
            "`babs submit` the cell's derivative, deploying every job that does "
            "not yet have results and leaving finished ones alone. Writes no "
            "column: submitted-ness is volatile, babs owns it, and the reconciler "
            "reads it live. Refuses a cell that is not scaffolded, or is merged."
        ),
    )
    _cell_verb(
        sub,
        "merge",
        cmd_merge,
        summary="consolidate one finished cell's results and record it done",
        description=(
            "`babs merge` the cell's per-job result branches in its output RIA, "
            "fast-forward the derivative onto the consolidated branch, and set the "
            "cell's `merged` column. Refuses a cell that is not scaffolded, is "
            "already merged, or whose live `babs status` counts do not say merge — "
            "merging a partial set would quietly produce a derivative that looks "
            "complete."
        ),
    )

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
