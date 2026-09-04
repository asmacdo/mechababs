"""status.py — the campaign at a glance: one row per cell, read-only.

The statefile is tall — one row per (source dataset x app config) cell — and so is
this: ``status`` renders the shard as it is, with each cell's state filled in. No
pivot, no per-job table. What it adds to the file is the part the file deliberately
does not store: for a cell that is running, the live job counts, asked of babs at the
moment you look.

**At a superstudy the shape is the same table, wider.** The rows span every member,
gaining a column for which member each came from and one for whether that member is
on disk; the rollup is computed from the member shards, never stored
(``report_superstudy``). That the same render serves both levels is the point — a
superstudy is a fan-out, not a second model.

**The table is stdout; the summary line is stderr** (``note``), so a pipe sees rows
and only rows.

Distinct from ``babs_status.py``, which parses one cell's ``babs status --json`` for
the reconciler to route on. This is for a human looking at the whole study at once.

**The state column is the reconciler's own reading.** It comes from ``iterate.route``
rather than a second interpretation of the same columns — a cell that ``iterate``
calls "waiting on X" and ``status`` called "not started" would be a bug that only
shows up when it matters.

**Read-only, and never in the way.** It takes no flock: the campaign lock is
exclusive, so holding it here would make looking at a campaign block until the iterate
finished — precisely when you most want to look. The cost is a torn read if a verb
rewrites the shard in the same instant, which fixes itself on the next invocation.
Nothing here writes campaign state, so observability costs no provenance.
"""

import subprocess
import sys
from collections import Counter
from pathlib import Path

from mechababs import babs_status, iterate
from mechababs import campaign as campaign_mod
from mechababs import scaffold as scaffold_mod
from mechababs import study as study_mod

COLUMNS = [
    "source_dataset",
    "app",
    "level",
    "subjects",
    "sessions",
    "state",
    "jobs",
]

# At a superstudy the same table gains two columns: which member a row came from, and
# whether that member is on disk. Prepended rather than appended -- they are the
# coarsest identity on the row, and the eye groups by the left edge.
#
# `installed` is its own column and NOT a state, because the two axes are
# independent: a member whose cells are all merged and whose content has since been
# dropped is finished AND absent, and folding those into one column would report it
# as neither.
STUDY = "study"
INSTALLED = "installed"
SUPER_COLUMNS = [STUDY, INSTALLED, *COLUMNS]
YES, NO = "yes", "no"

# What a cell's state reads as. The four routed states, plus the one a human has to
# act on: an active cell whose live counts say jobs failed. It is called out rather
# than left as "active" because it is the only row on the table that is stuck.
NOT_STARTED = "not started"
MERGED = "merged"
ACTIVE = "active"
FAILED = "FAILED"

# A member with no working tree at all (never installed, or `datalad uninstall`ed):
# its shard is not on disk, so its cells' states are *unreadable*, and "not started"
# about work that may well be finished would be a lie. Its rows read the catalog's
# `lifecycle` instead; `unknown` is the fallback for a row that has none. A member
# whose annexed *content* was dropped keeps its git repo, and the campaign dir is
# `annex.largefiles=nothing`, so its shard still reads exactly right.
UNKNOWN = "unknown"

# The catalog's lifecycle, said in the table's own words, so `state` means one thing
# whichever file it was read from. Exact at both ends and lossy in the middle: a
# member-level `active` cannot say which cell is the active one, which is the
# resolution the catalog claims and the reason installing the member buys detail.
FROM_LIFECYCLE = {
    campaign_mod.LIFECYCLE_REGISTERED: NOT_STARTED,
    campaign_mod.LIFECYCLE_ACTIVE: ACTIVE,
    campaign_mod.LIFECYCLE_MERGED: MERGED,
}

# ``route`` returns a waiting state carrying its producer's stem, so the summary
# buckets on the prefix rather than the whole string.
WAITING = "waiting"

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

    ``derivative`` is carried but not in ``COLUMNS``: it is the widest column on the
    table and says nothing a reader acts on (the path is derivable from the study,
    app and source dataset already shown), while ``cell_installed`` needs it to ask
    whether that derivative is on disk.
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


def cell_installed(study, record):
    """Is this cell's work on disk — the study AND the derivative it produced?

    One column, not two. To a reader the question is "can I open this", and a study
    that is present with its derivative offloaded cannot be opened any more than a
    study that is gone. So both halves fold into one answer, and `no` means "not all
    of it is here" rather than naming which half.

    Both halves are stats, so a whole-superstudy look stays cheap. What this does NOT
    see is dropped annex *content*: `datalad drop` leaves the derivative dataset in
    place, so the cell still reads `yes`. A cell with nothing scaffolded yet is `yes`
    when its study is — there is no derivative to be missing.
    """
    derivative = record.get("derivative") or ""
    if not derivative:
        return YES
    return YES if study_mod.is_study_root(Path(study) / derivative) else NO


def member_records(superstudy, label, name, catalog):
    """One member's rows, tagged with the member and whether it is on disk.

    An **uninstalled** member is rendered from the catalog rather than its shard, one
    row per selected source dataset (the same one-fact-per-row shape as the installed
    case), state from ``FROM_LIFECYCLE``. It costs no shard read and no babs query,
    which is what keeps a whole-superstudy ``status`` cheap when most of it is not on
    disk.
    """
    path = Path(superstudy) / name
    if study_mod.is_study_root(path):
        return [
            {STUDY: name, INSTALLED: cell_installed(path, record), **record}
            for record in records(path, label)
        ]
    return [
        {
            **{column: "" for column in SUPER_COLUMNS},
            STUDY: name,
            INSTALLED: NO,
            "source_dataset": row.get("source_dataset", ""),
            "state": FROM_LIFECYCLE.get(row.get("lifecycle"), UNKNOWN),
        }
        for row in catalog
        if row.get("study") == name
    ]


# The order the counts read in: what is finished, what is moving, what is stuck, what
# has not begun, what cannot be seen. FAILED sits third, where it is read before the
# eye stops; UNKNOWN sits last, since it is a fact about us rather than about a cell.
SUMMARY_ORDER = [MERGED, ACTIVE, FAILED, WAITING, NOT_STARTED, UNKNOWN]


def summarize(data):
    """The cell counts, as one clause — only the buckets that have anything in them.

    Zero buckets are dropped rather than printed, so the line stays short enough to
    read at a glance on a long sweep. The cost is that "0 failed" is absent rather
    than reassuring; ``FAILED`` appearing at all is the signal, and its absence is the
    ordinary case.
    """
    counts = Counter(
        WAITING if row["state"].startswith("waiting on ") else row["state"]
        for row in data
    )
    total = sum(counts.values())
    parts = [f"{counts[state]} {state}" for state in SUMMARY_ORDER if counts[state]]
    return f"{total} cell(s)" + (f": {', '.join(parts)}" if parts else "")


def narrow(data, app, root, label):
    """``data`` cut to one app's cells, by stem. Returns the rows to render.

    Validated against the campaign's **declared** vocabulary, not against
    the apps visible in ``data`` — so a typo is refused even when every member is
    uninstalled and there is not a single readable cell to compare against. Naming an
    app the campaign does not have is a typo far more often than it is an empty
    campaign, the same judgement ``iterate.work_list`` makes.

    **Rows for a member we cannot see into survive the filter.** Their app is
    unreadable, not absent, so dropping them would assert that this app has no cell
    there — the claim ``unknown`` exists to avoid making. A typo cannot hide behind
    them, because the name was already checked against the declaration.
    """
    if app is None:
        return data
    campaign_mod.require_declared_app(root, label, app)
    return [row for row in data if row["app"] == app or row["state"] == UNKNOWN]


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


def run_status(root=".", *, study=None, app=None):
    """Resolve where we are standing, then report. Returns a CLI exit code.

    Same split as the reconciler's: ``run_status`` answers "which study, which
    campaign, and is this the right environment", and the ``report`` functions take
    both as parameters. The level is resolved the way ``iterate`` does it (where you
    stand gives the level, ``study`` narrows within it), so the two commands agree
    about what a campaign contains. No flock: see the module docstring.
    """
    selected = campaign_mod.require_selected_campaign(root)
    root, label = selected.root, selected.label
    if not campaign_mod.is_superstudy_campaign(root, label):
        if study:
            sys.exit(
                f"campaign {label!r} here is configured at a study, so there are "
                f"no members to select between.\n--study narrows a superstudy view."
            )
        return report(root, label, app=app)
    return report_superstudy(root, label, study=study, app=app)


def note(text):
    """The summary line, on stderr — commentary, not data.

    stdout stays the table alone, so `status | grep FAILED` greps rows and
    `status > cells.tsv` writes a file with nothing to strip.
    """
    print(text, file=sys.stderr)


def report(study, label, *, app=None):
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
    data = narrow(data, app, study, label)
    note(f"campaign {label!r} · study {Path(study).name} · {summarize(data)}")
    sys.stdout.write(render(data))
    return 0


def report_superstudy(superstudy, label, *, study=None, app=None):
    """Render every member's cells for ``label``, in catalog order. A CLI exit code.

    The rollup is **computed, never stored** — read out of the member shards at the
    moment you look, the same way ``iterate`` re-reads ground truth each run. The
    superstudy commits membership and nothing else, so there is no cached per-cell
    state here that could drift out of agreement with the shards it summarizes.

    ``member_studies`` does the narrowing, so ``study`` matches the catalog rather
    than the filesystem, and naming a directory that was never selected into this
    campaign is an error here exactly as it is for ``iterate``.
    """
    members = iterate.member_studies(superstudy, label, study)
    catalog = campaign_mod.read_members(superstudy, label)

    data = []
    for name in members:
        data.extend(member_records(superstudy, label, name, catalog))
    if not data:
        print(
            f"campaign {label!r} has no members yet — `mechababs add-dataset "
            f"--study <member|url> --sourcedata <path>` selects the data it acts on.",
            file=sys.stderr,
        )
        return 0

    data = narrow(data, app, superstudy, label)

    installed = sum(
        1 for name in members if study_mod.is_study_root(Path(superstudy) / name)
    )
    note(f"campaign {label!r} · superstudy {Path(superstudy).name}")
    note(f"{len(members)} member(s), {installed} installed · {summarize(data)}")
    sys.stdout.write(render(data, SUPER_COLUMNS))
    return 0
