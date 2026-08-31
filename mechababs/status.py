"""status.py — the campaign at a glance: one row per cell, read-only.

The statefile is tall — one row per (source dataset x app config) cell — and so is
this: ``status`` renders the shard as it is, with each cell's state filled in. No
pivot, no per-job table. What it adds to the file is the part the file deliberately
does not store: for a cell that is running, the live job counts, asked of babs at the
moment you look.

Distinct from ``babs_status.py``, which parses one cell's ``babs status --json`` for
the reconciler to route on. This is for a human looking at the whole study at once.

**The state column is the reconciler's own reading.** It comes from ``iterate.route``
rather than a second interpretation of the same columns — a cell that ``iterate``
calls "waiting on X" and ``status`` called "not started" would be a bug that only
shows up when it matters.

**Read-only, and never in the way.** It takes no flock: the campaign lock is
exclusive, so holding it here would make looking at a campaign block until the tick
finished — precisely when you most want to look. The cost is a torn read if a verb
rewrites the shard in the same instant, which fixes itself on the next invocation.
Nothing here writes campaign state, so observability costs no provenance.
"""

import subprocess
import sys
from pathlib import Path

from mechababs import babs_status, iterate
from mechababs import campaign as campaign_mod
from mechababs import scaffold as scaffold_mod

COLUMNS = [
    "source_dataset",
    "app",
    "level",
    "subjects",
    "sessions",
    "state",
    "jobs",
    "derivative",
]

# What a cell's state reads as. The four routed states, plus the one a human has to
# act on: an active cell whose live counts say jobs failed. It is called out rather
# than left as "active" because it is the only row on the table that is stuck.
NOT_STARTED = "not started"
MERGED = "merged"
ACTIVE = "active"
FAILED = "FAILED"

# When babs cannot be asked about a cell (a moved project, a broken derivative). One
# unreadable cell must not cost the view of the others, so it is reported in place.
UNAVAILABLE = "babs status unavailable"


def cell_jobs(project):
    """The live counts for one active cell, or why they could not be read.

    ``ValueError`` covers unparsable output (``json.JSONDecodeError`` is one), the
    other two a babs that exited non-zero or is not there at all.
    """
    try:
        status = babs_status.read_status(project)
    except (subprocess.CalledProcessError, OSError, ValueError):
        return UNAVAILABLE, None
    return iterate.describe_counts(status), status


def cell_record(study, rows, row):
    """One rendered row: the shard's identity columns, plus the derived state.

    Only an active cell costs a babs query — a merged or not-yet-started one has
    nothing volatile to ask about, which is the same economy the reconciler makes.
    """
    state, detail = iterate.route(rows, row)
    record = {
        "source_dataset": row.get("source_dataset", ""),
        "app": scaffold_mod.app_stem(row.get("app_config", "")),
        "level": row.get("processing_level", ""),
        "subjects": row.get("n_subjects", ""),
        "sessions": row.get("n_sessions", ""),
        "state": NOT_STARTED,
        "jobs": "",
        "derivative": row.get("babs", ""),
    }
    if state == iterate.DONE:
        record["state"] = MERGED
    elif state == iterate.WAITING:
        record["state"] = f"waiting on {detail}"
    elif state == iterate.ACTIVE:
        jobs, status = cell_jobs(Path(study) / detail)
        record["jobs"] = jobs
        record["state"] = (
            FAILED if status and babs_status.decide(status) == "fail" else ACTIVE
        )
    return record


def records(study, label):
    """Every cell in the shard, in file order, rendered."""
    rows = campaign_mod.read_state(study, label)
    return [cell_record(study, rows, row) for row in rows]


def render(data, columns=COLUMNS):
    """The aligned table, as text.

    Aligned here rather than piped through ``column -t``: the table is small, the
    alignment is four lines, and a status command should not fail (or silently change
    shape) because a coreutils binary is missing from a container.
    """
    widths = {
        col: max([len(col)] + [len(str(row.get(col, ""))) for row in data])
        for col in columns
    }
    lines = []
    for row in [{col: col for col in columns}, *data]:
        cells = [str(row.get(col, "")).ljust(widths[col]) for col in columns]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines) + "\n"


def run_status(root="."):
    """Resolve where we are standing, then report. Returns a CLI exit code.

    Same split as the reconciler's: ``run_status`` answers "which study, which
    campaign, and is this the right environment", and ``report`` takes both as
    parameters — so nothing below here assumes the study is the cwd.
    """
    study, label, _, _ = campaign_mod.require_selected_campaign(root)
    return report(study, label)


def report(study, label):
    """Render ``study``'s cells for ``label``. Returns a CLI exit code."""
    campaign_mod.require_statefile(study, label)

    data = records(study, label)
    if not data:
        print(
            f"campaign {label!r} has no cells yet — `mechababs add-dataset "
            f"--sourcedata <path>` selects the data it acts on.",
            file=sys.stderr,
        )
        return 0
    sys.stdout.write(render(data))
    return 0
