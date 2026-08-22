"""status.py — the campaign-wide job table (read-only observation).

Backs ``mechababs status``. Distinct from ``babs_status.py``, which parses one
cell's ``babs status --json`` for the reconciler to decide on; this one is for a
human looking at the whole campaign at once.

babs tracks jobs per-cell in each project's ``code/job_status.csv``, which carries no
dataset/pipeline column and names every job ``bid`` — so answering "which job was
that dataset's failing subject?" otherwise means log-filename -> ``sacct``
gymnastics. This aggregates every babs project's CSV across the campaign into ONE
table, tagging each row with its dataset + pipeline (derived from the path) and
computing each job's stderr log path, so a failure points straight at its log.

Read-only by construction: it never writes campaign state, so it costs no
provenance — observability is free, unlike a run-config change.

The CSV is a *cache* babs recomputes from ``sacct``. By default each matched cell is
refreshed (``babs status``) before rendering so ``state``/``time_used``/``is_failed``
are live; ``--no-refresh`` skips that and accepts what's on disk. Refresh is
deliberately the default: a stale row can show a *running* job as failed (babs's
submit path rewrites a resubmitted row's job_id without clearing the prior attempt's
``is_failed``), so reading the cache is an explicit choice, not an accident.

Columns are read **by name** (``csv.DictReader``), never by position — babs's CSV is
not a contracted API, so a reordering is safe here and a missing column renders
empty rather than crashing.
"""

import csv
import io
import subprocess
import sys
from glob import glob
from pathlib import Path

COLUMNS = [
    "dataset",
    "pipeline",
    "sub_id",
    "ses_id",
    "job_id",
    "task_id",
    "state",
    "time_used",
    "time_limit",
    "is_failed",
    "has_results",
    "log",
]

# babs names every job `bid`; used when the CSV's `name` isn't populated yet.
DEFAULT_JOB_NAME = "bid"

# `columns`/`vd` hand the TSV to a viewer so the caller needn't remember the
# `column -t -s $'\t'` incantation; `tsv` is the pipe-anywhere data form.
_RENDERERS = {
    "columns": ["column", "-t", "-s", "\t"],
    "vd": ["vd", "-f", "tsv"],
}


def cells(campaign: Path, study=None, derivative=None):
    """Yield (csv_path, dataset, pipeline) for each babs project, filtered.

    Only babs projects match: a study's *published* derivatives (no babs scaffold)
    have no ``code/job_status.csv``, so the glob skips them.
    """
    want_dataset = study.removeprefix("study-") if study else None
    pattern = str(
        campaign / "studies" / "*" / "derivatives" / "*" / "code" / "job_status.csv"
    )
    for csv_path in sorted(glob(pattern)):
        cell = Path(csv_path).parent.parent  # .../derivatives/<pipeline>
        pipeline = cell.name
        dataset = cell.parent.parent.name.removeprefix("study-")
        if want_dataset and dataset != want_dataset:
            continue
        if derivative and pipeline != derivative:
            continue
        yield csv_path, dataset, pipeline


def refresh(matched):
    """Recompute each matched cell's job_status.csv via ``babs status`` (slow).

    Filtering narrows this first, so ``--study X`` gets live state for one dataset
    without paying for the whole campaign.
    """
    total = len(matched)
    for i, (csv_path, dataset, pipeline) in enumerate(matched, 1):
        cell = str(Path(csv_path).parent.parent)
        print(f"refreshing {i}/{total}: {dataset}/{pipeline}", file=sys.stderr)
        try:
            subprocess.run(["babs", "status", cell], capture_output=True, text=True)
        except FileNotFoundError:
            print(
                "`babs` not on PATH; skipping refresh (use --no-refresh)",
                file=sys.stderr,
            )
            return


def rows(matched):
    for csv_path, dataset, pipeline in matched:
        with open(csv_path, newline="") as fh:
            for record in csv.DictReader(fh):
                job_id = record.get("job_id", "")
                task_id = record.get("task_id", "")
                name = record.get("name") or DEFAULT_JOB_NAME
                log = (
                    f"studies/study-{dataset}/derivatives/{pipeline}"
                    f"/logs/{name}.e{job_id}_{task_id}"
                    if job_id
                    else ""
                )
                row = {col: (record.get(col) or "") for col in COLUMNS}
                row.update(dataset=dataset, pipeline=pipeline, log=log)
                yield row


def _sort_key(row):
    task = row["task_id"]
    return (row["dataset"], row["pipeline"], int(task) if task.isdigit() else 0)


def render(data, output):
    """Emit the table. TSV is the data form; `columns`/`vd` hand it to a viewer."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, delimiter="\t")
    writer.writeheader()
    writer.writerows(data)
    tsv = buf.getvalue()
    if output == "tsv":
        sys.stdout.write(tsv)
        return
    cmd = _RENDERERS[output]
    try:
        subprocess.run(cmd, input=tsv, text=True)
    except FileNotFoundError:
        print(f"`{cmd[0]}` not on PATH; emitting tsv", file=sys.stderr)
        sys.stdout.write(tsv)


def run_status(
    campaign,
    *,
    study=None,
    derivative=None,
    only_failed=False,
    do_refresh=True,
    output="columns",
):
    """Render the campaign's job table. Returns a CLI exit code."""
    matched = list(cells(campaign, study, derivative))
    if not matched:
        print("no matching babs cells found", file=sys.stderr)
        return 1
    if do_refresh:
        refresh(matched)
    data = sorted(rows(matched), key=_sort_key)
    if only_failed:
        data = [row for row in data if row["is_failed"].lower() == "true"]
    render(data, output)
    return 0
