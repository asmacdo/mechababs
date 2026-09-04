"""merge.py — the last transition: consolidate a finished cell's results, and record it done.

Every job in the cell has ended successfully, and each one pushed its results to
the derivative's output RIA on a branch of its own. Merging is what turns that pile
of branches into the derivative: ``babs merge`` consolidates them in the RIA, and
then the derivative is fast-forwarded onto the result — because ``babs merge``
pushes to the store and never touches the derivative's working tree, so without the
second half the results exist and the derivative looks untouched.

Then the cell's ``merged`` column is set, which is what routes every later iterate past
it without asking babs anything.

**Unambiguously mutating**, so unlike ``submit`` this one is dispatched under a
``datalad run``: it moves the derivative's HEAD (and with it the study's gitlink)
and rewrites the statefile row, which is exactly what the run declares as outputs.

**Self-guarding twice over.** The cell must be active (scaffolded, not already
merged) *and* the live counts must still say merge. The second is the one that
earns its keep: babs merges whatever result branches it finds, so a merge run too
early does not fail — it quietly produces a derivative that looks complete and is
not. A `datalad rerun` onto current HEAD, or a hand-run of the recorded command,
lands right there.
"""

import subprocess
import sys
from pathlib import Path

from datalad import api as datalad_api

from mechababs import babs_status
from mechababs import campaign as campaign_mod

# The sibling babs gives a derivative for its output RIA store. `babs merge` pushes
# the consolidated branch there; this is where we pull it back from.
OUTPUT_SIBLING = "output"

# What the `merged` column holds. Its PRESENCE is the state — nothing reads the
# value — so it says the obvious thing rather than a timestamp that would invite
# being parsed.
MERGED = "true"


def require_all_done(project, row):
    """Refuse unless the live babs counts still say this cell is ready to merge.

    Re-asked here rather than inherited from whoever routed us: the reconciler's
    decision is an iterate old at best, and for a rerun it is a different run's
    decision entirely.
    """
    status = babs_status.read_status(project)
    action = babs_status.decide(status)
    if action == "merge":
        return status
    sys.exit(
        f"{row['source_dataset']} / {Path(row['app_config']).stem} is not ready to "
        f"merge: babs says {action!r}.\n"
        f"  {status}\n"
        "Merging a partial set is not a loud failure — babs merges whatever result "
        "branches exist — so it is refused rather than discovered later in a "
        "derivative that looks complete."
    )


def pull_merged_results(project):
    """Fast-forward the derivative onto the branch ``babs merge`` pushed to the RIA.

    The results arrive as tracked annex symlinks; their content stays in the output
    RIA under the derivative's gitignored ``.babs/``, which is where babs keeps it
    and where a later ``datalad get`` finds it.
    """
    results = datalad_api.update(
        dataset=str(project),
        sibling=OUTPUT_SIBLING,
        how="merge",
        result_renderer="disabled",
        on_failure="ignore",
        return_type="list",
    )
    failed = [r for r in results if r.get("status") not in ("ok", "notneeded")]
    if failed:
        sys.exit(
            f"could not merge {OUTPUT_SIBLING} into {project}\n"
            + "\n".join(f"  {r.get('status')}: {r.get('path')}" for r in failed)
            + "\nbabs consolidated the results in the output RIA; they are not lost, "
            "but the derivative does not carry them yet."
        )


def merge(study, label, source_dataset, app_config):
    """Merge one finished cell's results and record it done. Returns its ``babs`` path.

    Called inside the dispatcher's ``datalad run``; commits nothing at the study
    itself.
    """
    study = Path(study)
    rows = campaign_mod.read_state(study, label)
    row = campaign_mod.find_cell(rows, source_dataset, app_config)
    project = campaign_mod.require_active_cell(row, "merge")
    require_all_done(study / project, row)

    cmd = [campaign_mod.babs_bin(), "merge", project]
    print("+ " + " ".join(cmd), file=sys.stderr)
    subprocess.run(cmd, cwd=str(study), check=True)
    pull_merged_results(study / project)

    # The cell's second and last durable fact. Its presence is what makes every
    # later iterate skip this cell without querying babs at all.
    row["merged"] = MERGED
    campaign_mod.write_state(study, label, rows)
    return project
