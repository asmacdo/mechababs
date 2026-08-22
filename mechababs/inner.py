"""mechababs-inner — the action verbs the reconciler dispatches. Not a user CLI.

`mechababs` is what a person runs; `mechababs-inner` is what a `datalad run`
records. The split exists so the two can have different manners:

- **self-labeling.** Seeing `mechababs-inner scaffold …` in a study's history says
  unambiguously "a machine-dispatched provenance step", not "someone typed this".
- **self-guarding.** Each verb refuses a cell that is not in the state it advances
  from. A bare `datalad rerun` onto current HEAD re-executes the recorded command
  against a cell that has since been scaffolded, and the desired outcome there is
  a loud failure, not a second derivative.
- **no configured-level check.** That check ("operate a campaign only from where it
  was configured") lives on `iterate`, deliberately: user-driven advancing is
  gated, while reproducing a recorded run is not.

The campaign is a required flag rather than the `MECHABABS_CAMPAIGN` env var, so a
recorded command names what it operated on instead of inheriting it. The env-match
guard still applies: a run recorded as this campaign's has to be executed by this
campaign's environment.
"""

import argparse
import sys

from mechababs import __version__
from mechababs import campaign as campaign_mod
from mechababs import scaffold as scaffold_mod
from mechababs import study as study_mod


def cmd_scaffold(args):
    """Advance one cell from "not started" to "initialized" (see scaffold.py)."""
    study = study_mod.require_study_root(".")
    campaign_mod.require_statefile(study, args.campaign)
    campaign_mod.require_env_match(study, args.campaign)
    project = scaffold_mod.scaffold(study, args.campaign, args.source_dataset, args.app)
    print(
        f"scaffolded {args.source_dataset} / "
        f"{scaffold_mod.app_stem(args.app)} -> {project}",
        file=sys.stderr,
    )
    return 0


def main():
    p = argparse.ArgumentParser(
        prog="mechababs-inner",
        description=__doc__.split("\n\n")[0],
        epilog="Dispatched by `mechababs iterate` under `datalad run`; not a "
        "command to run by hand.",
    )
    p.add_argument("--version", action="version", version=f"mechababs {__version__}")
    sub = p.add_subparsers(dest="verb", required=True)

    ps = sub.add_parser(
        "scaffold",
        help="init one cell's derivative and record it",
        description=(
            "Generate the cell's subject inclusion, compose the babs config from "
            "the campaign's app x cluster x source axes, `babs init` the "
            "derivative into the study's derivatives/, pin the requested subject "
            "list, and record the derivative's path in the cell's `babs` column. "
            "Refuses a cell that is already scaffolded, is not in the statefile, "
            "or is waiting on an unmerged producer."
        ),
    )
    ps.add_argument(
        "--campaign",
        required=True,
        metavar="LABEL",
        help="the campaign whose statefile holds the cell. A flag, not "
        "the env var: a recorded command names what it ran on.",
    )
    ps.add_argument(
        "--source-dataset",
        required=True,
        metavar="PATH",
        help="the cell's source dataset, study-relative (e.g. sourcedata/ds000001)",
    )
    ps.add_argument(
        "--app",
        required=True,
        metavar="PATH",
        help="the cell's app config, campaign-relative "
        "(e.g. bids-app-configs/MRIQC-24.0.2.yaml)",
    )
    ps.set_defaults(func=cmd_scaffold)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
