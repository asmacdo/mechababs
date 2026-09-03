"""jobs.py — one row per job, read-only: the drill-down under ``status``'s cells.

``status`` answers "how is each cell doing"; this answers "which subject's job
failed, and where is its log". Separate commands rather than a flag, because they
are different questions with different rows: a cell table stays readable across a
whole superstudy, and a job table is what you open once you know which cell to look
at.

**babs's ``job_status.csv`` is the source, and it is not a contracted API.** It
carries no dataset or app column and names every job ``bid``, so on its own a
failure is a log filename you have to trace back by hand. What this adds is the
context babs has no way to know: which study, which source dataset, which app each
row came from, and the log path assembled from the pieces. Columns are read **by
name** (``csv.DictReader``) so a reordering upstream is harmless and a renamed
column renders empty rather than crashing.

**A cell with no ``job_status.csv`` is left out entirely.** Nothing has been
submitted there, so it has no jobs — a placeholder row would be a cell table's
answer printed in a job table's shape.

**The CSV is a cache babs recomputes from the scheduler, so it is refreshed first.**
Reading it as-is can be actively wrong, not merely stale: babs's submit path rewrites
a resubmitted row's ``job_id`` without clearing the previous attempt's ``is_failed``,
so a running job can show as failed. ``--no-refresh`` makes accepting that an
explicit choice. Scoping narrows *before* the refresh, so ``--study`` on one member
costs one member's queries rather than the whole superstudy's.

Read-only and lock-free, exactly as ``status`` is: looking at a campaign must never
block behind an iterate. Refreshing writes only inside babs's own project — no campaign
state, so observability still costs no provenance.
"""

import csv
import subprocess
import sys
from pathlib import Path

from mechababs import babs_status
from mechababs import campaign as campaign_mod
from mechababs import scaffold as scaffold_mod
from mechababs import status as status_mod
from mechababs import study as study_mod

# babs writes `ses_id` only for a session-level project, so the column is present
# for some cells and absent for others in the same table. Rendering is by name with
# a default, so a subject-level row simply leaves it empty.
COLUMNS = [
    "source_dataset",
    "app",
    "sub_id",
    "ses_id",
    "job_id",
    "state",
    "time_used",
    "time_limit",
    "failed",
    "logs",
]
SUPER_COLUMNS = [status_mod.STUDY, *COLUMNS]

# Where babs keeps the per-job cache inside a derivative, and where the logs land.
JOB_STATUS_CSV = Path("code") / "job_status.csv"
LOGS_DIRNAME = "logs"

TRUE = "true"


def job_ref(record):
    """The job as SLURM itself addresses it: ``<job id>_<task id>`` for an array.

    Not a mechababs invention — it is what ``sacct`` and ``squeue`` print in their
    JobID column, and what ``sacct -j`` and ``scontrol show job`` accept back, so the
    value is paste-ready rather than two halves to reassemble. babs stores them
    separately; a job with no array index is just its id, and an unsubmitted one has
    neither.
    """
    job_id = record.get("job_id") or ""
    task_id = record.get("task_id") or ""
    if not job_id:
        return ""
    return f"{job_id}_{task_id}" if task_id else job_id


def cell_jobs(study, row, prefix=""):
    """Every job of one cell, or nothing at all if it has no ``job_status.csv``.

    ``prefix`` is the member directory at a superstudy, so the log path resolves
    from where the command was run rather than from inside the member.
    """
    derivative = row.get("babs") or ""
    if not derivative:
        return []
    csv_path = Path(study) / derivative / JOB_STATUS_CSV
    if not csv_path.is_file():
        return []
    shown = str(Path(prefix) / derivative) if prefix else derivative
    jobs = []
    with open(csv_path, newline="") as fh:
        for record in csv.DictReader(fh):
            jobs.append(
                {
                    "source_dataset": row.get("source_dataset", ""),
                    "app": scaffold_mod.app_stem(row.get("app_config", "")),
                    "sub_id": record.get("sub_id", ""),
                    "ses_id": record.get("ses_id", ""),
                    "job_id": job_ref(record),
                    "state": record.get("state", ""),
                    "time_used": record.get("time_used", ""),
                    "time_limit": record.get("time_limit", ""),
                    "failed": (record.get("is_failed") or "").lower(),
                    "logs": str(Path(shown) / LOGS_DIRNAME),
                }
            )
    return jobs


def scoped_cells(root, label, *, superstudy, study=None, app=None):
    """The cells in scope, and the members that could not be looked at.

    Returns ``(cells, unknown)``: ``cells`` is ``[(prefix, study_path, row)]`` for
    every in-scope cell that has a derivative, and ``unknown`` names the members with
    no working tree. A cell with no derivative is dropped here — selected but never
    scaffolded, so there is nothing to refresh and nothing to read.

    Scoping happens here, before anything is refreshed or read, which is what keeps
    ``--study`` on one member from costing the whole superstudy's queries.
    """

    def cells_of(study_path, prefix=""):
        for row in campaign_mod.read_state(study_path, label):
            stem = scaffold_mod.app_stem(row.get("app_config", ""))
            if app and stem != app:
                continue
            if row.get("babs"):
                yield prefix, study_path, row

    if not superstudy:
        return list(cells_of(root)), []

    cells, unknown = [], []
    for name in status_mod.iterate.member_studies(root, label, study):
        member = Path(root) / name
        # An uninstalled member contributes no rows: `status` is where absence is a
        # fact worth a row, and a job table has nothing to say about jobs it cannot
        # see. It is still counted, so an empty result can say "unknown" rather than
        # "nothing submitted" — the two look identical from here, only one is true.
        if not study_mod.is_study_root(member):
            unknown.append(name)
            continue
        cells.extend(cells_of(member, name))
    return cells, unknown


def refresh(cells):
    """Recompute each scoped cell's ``job_status.csv`` from the scheduler.

    Asking the pinned babs for a cell's status is what rewrites its CSV, so the
    existing ``babs status --json`` call doubles as the refresh rather than needing a
    second way to invoke babs. Its counts are discarded — the rows are what we came
    for.

    A cell babs cannot answer about is skipped rather than fatal: its CSV is then read
    as it stands, which is the ``--no-refresh`` outcome for that one cell. One broken
    project must not cost the view of the others.
    """
    for i, (_, study_path, row) in enumerate(cells, 1):
        status_mod.note(f"refreshing {i}/{len(cells)}: {row['babs']}")
        try:
            babs_status.read_status(Path(study_path) / row["babs"])
        except (subprocess.CalledProcessError, OSError, ValueError):
            continue


def run_jobs(root=".", *, study=None, app=None, failed=False, refresh_first=True):
    """Resolve the level, gather every job in scope, render. A CLI exit code.

    Same level resolution as ``status`` and ``iterate`` — where you stand gives the
    level, ``study`` narrows within it — so the three commands never disagree about
    what a campaign contains.
    """
    selected = campaign_mod.require_selected_campaign(root)
    root, label = selected.root, selected.label
    if app:
        campaign_mod.require_declared_app(root, label, app)

    superstudy = campaign_mod.is_superstudy_campaign(root, label)
    if not superstudy:
        if study:
            sys.exit(
                f"campaign {label!r} here is configured at a study, so there are "
                f"no members to select between.\n--study narrows a superstudy view."
            )
        campaign_mod.require_statefile(root, label)
    columns = SUPER_COLUMNS if superstudy else COLUMNS
    where = ("superstudy " if superstudy else "study ") + Path(root).name

    cells, unknown = scoped_cells(
        root, label, superstudy=superstudy, study=study, app=app
    )
    if refresh_first:
        refresh(cells)

    data = []
    for prefix, study_path, row in cells:
        found = cell_jobs(study_path, row, prefix)
        data.extend(
            ({status_mod.STUDY: prefix, **j} for j in found) if superstudy else found
        )

    if failed:
        data = [j for j in data if j["failed"] == TRUE]

    if not data:
        # Two very different empties, and saying the wrong one sends you looking in
        # the wrong place: nothing has been submitted, or it has and we cannot see.
        if unknown:
            status_mod.note(
                f"campaign {label!r} · {where} · unknown — "
                f"{', '.join(unknown)} not installed, so there is no shard to read. "
                f"`mechababs status` shows what is known without it."
            )
            return 0
        scope = " matching" if (failed or app or study) else ""
        status_mod.note(
            f"campaign {label!r} · {where} · no{scope} jobs yet — a cell with "
            f"nothing submitted has no job_status.csv to read."
        )
        return 0

    failures = sum(1 for j in data if j["failed"] == TRUE)
    tally = f"{len(data)} job(s)" + (f", {failures} failed" if failures else "")
    # An unreadable member makes this a partial answer, and a total that silently
    # omits members reads as complete. Say so rather than let it.
    if unknown:
        tally += f" ({len(unknown)} member(s) not installed, their jobs unknown)"
    if not refresh_first:
        tally += " (not refreshed)"
    status_mod.note(f"campaign {label!r} · {where} · {tally}")
    sys.stdout.write(status_mod.render(data, columns))
    return 0
