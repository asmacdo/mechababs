"""iterate.py — the reconciler.

Every verb in a cell's life exists on its own (scaffold, submit, merge), and each one
refuses a cell that is not in the state it advances from. ``iterate`` is what decides
*which* verb a cell is owed, and dispatches it: one ``iterate`` reads the statefile
shard and visits every cell once, advancing each by **at most one transition**. One
cell advancing is a **tick**; a cell that is passed over is not one, and does not
count against ``--batch``.

**Level-triggered, not edge-triggered.** An ``iterate`` never remembers what the last
one did. It re-reads ground truth — the shard's columns, plus a live ``babs status``
for any cell that is running — and re-derives what each cell needs from that alone.
So a crashed ``iterate``, a hand-edited shard, or a cell repaired by hand between
runs all converge on the next one instead of accumulating drift. This is also why there is no
status enum: state is *read off* ``babs``/``merged``, and anything volatile is asked
of babs at the moment it is needed.

The routing, which is the whole of the reconciler's opinion:

===================  ==========================================================
 ``merged`` set       done — skipped without asking babs anything
 ``babs`` set         active — ``babs status`` counts decide submit/skip/merge/fail
 neither, gated       waiting on an unmerged producer — **noted, and moved past**
 neither, clear       not started — scaffold
===================  ==========================================================

**Gating is noting, not blocking.** A dependent cell whose producer has not merged is
not a halt: ``iterate`` says so and goes on to the next cell. The next one re-checks.

**A failure is flagged, never merged, and never persisted.** When the live counts say
jobs failed, the cell is marked loudly and left alone — merging a partial set would
quietly produce a derivative that looks complete. The flag is this ``iterate``'s reading, not
a column, so a repair-and-resubmit (docs/interventions.md) takes effect with nothing
to clear.

**iterate is a plain coordinator.** It is not itself wrapped in a ``datalad run`` — it
dispatches one run per advancing cell, so runs never nest at the same level — and it
writes no statefile columns: the verbs do that, inside their own runs. The single
writer is enforced by the campaign flock, taken in exactly one place: around the whole
``iterate``, at the level the campaign is operated from.
"""

import sys
from pathlib import Path
from typing import NamedTuple

from mechababs import babs_status, dispatch
from mechababs import campaign as campaign_mod
from mechababs import scaffold as scaffold_mod
from mechababs import study as study_mod
from mechababs import utils
from mechababs.utils import require_clean_shallow

# Every line iterate writes about its own reasoning carries this, so mechababs'
# decisions stand out from the datalad, babs and git output they interleave with.
# The `+ <command>` echoes from the verbs are left unprefixed on purpose: those are
# commands, not commentary.
PREFIX = "mechababs>"

# The four cell states, read off the shard's columns. There is no status enum in the
# statefile; these are names for what the columns already say.
DONE = "done"
ACTIVE = "active"
WAITING = "waiting"
SCAFFOLD = "scaffold"

# What a cell was just *done to*, as distinct from what state it is in: the
# transition that happened, in the past tense a save message wants.
SCAFFOLDED = "scaffolded"
SUBMITTED = "submitted"
MERGED = "merged"

# The transitions that move git state in the study — a scaffold registers the
# derivative and a merge advances its HEAD — and so move the member's gitlink, which
# only the super can record. Submit is not one: it hands jobs to the scheduler and
# writes nothing the study tracks (``dispatch.plain``), so there is nothing to commit
# at either level.
RECORDED = (SCAFFOLDED, MERGED)


class Advance(NamedTuple):
    """One cell moved: what was done to it, which cell, and whose lifecycle it bears on.

    ``source_dataset`` is carried separately from ``cell`` — which is a display string —
    because it is the catalog key the lifecycle recompute needs, and re-parsing it back
    out of the label would be inventing a format to then depend on.
    """

    transition: str
    cell: str
    source_dataset: str


def note(text):
    """One prefixed line on stderr — iterate's own voice."""
    print(f"{PREFIX} {text}", file=sys.stderr)


def cell_label(row):
    """How a cell is named in output: its source dataset and its app's stem."""
    return f"{row['source_dataset']} / {scaffold_mod.app_stem(row['app_config'])}"


def producer_row(rows, row):
    """The row of this cell's ``depends_on`` producer, or ``None`` if there is none.

    The gate is a **shard-local row lookup** — the same source dataset's
    upstream-``app_config`` row — which is what keeps an edge from ever crossing
    studies: the reconciler only looks inside the shard it is reconciling.
    """
    upstream = row.get("depends_on") or ""
    key = (row["source_dataset"], upstream)
    for candidate in rows:
        if campaign_mod.cell_key(candidate) == key:
            return candidate
    return None


def route(rows, row):
    """This cell's state, from the shard alone: ``(state, detail)``.

    Pure — no babs, no filesystem — because it is the one reading of the columns that
    both ``iterate`` and ``status`` use, and two readings of the same columns would
    eventually disagree. ``detail`` is the derivative path for an active cell and the
    producer's stem for a waiting one; empty otherwise.

    The live job counts are deliberately NOT consulted here. Routing to ``active``
    says a cell has a babs project and is not merged; deciding what to do about it
    needs babs, and that is the caller's step (``babs_status.decide``).
    """
    if row.get("merged"):
        return DONE, ""
    if row.get("babs"):
        return ACTIVE, row["babs"]

    upstream = row.get("depends_on") or ""
    if not upstream:
        return SCAFFOLD, ""
    stem = scaffold_mod.app_stem(upstream)
    producer = producer_row(rows, row)
    if producer is None:
        # add-dataset refuses to write a cell whose producer has no row, so this is a
        # hand-edited shard. One broken cell must not take the iterate down: say what is
        # wrong and let the others advance.
        return WAITING, f"{stem} (no cell for it in this shard)"
    if not producer.get("merged"):
        return WAITING, stem
    return SCAFFOLD, ""


def source_lifecycle(rows, source_dataset):
    """One catalog row's lifecycle, derived from the shard it describes.

    Goes through ``route`` rather than reading ``babs``/``merged`` again, for the same
    reason ``status`` does: a second reading of the same columns is a second thing to
    keep in agreement, and this one would be committed.

    Derived, never accumulated: a stored view of the shard, not a tally the
    transitions keep, and not a repair pass hunting for drift behind the tool.
    """
    states = [
        route(rows, row)[0]
        for row in rows
        if campaign_mod.cell_key(row)[0] == source_dataset
    ]
    if states and all(state == DONE for state in states):
        return campaign_mod.LIFECYCLE_MERGED
    if any(state in (DONE, ACTIVE) for state in states):
        return campaign_mod.LIFECYCLE_ACTIVE
    return campaign_mod.LIFECYCLE_REGISTERED


def update_lifecycle(superstudy, label, name, member, advance):
    """Recompute the catalog lifecycle for the source dataset this transition touched.

    Returns ``{source_dataset: (before, after)}`` for the row if it changed, and writes
    it. Only the touched row is recomputed: a lifecycle changes as a consequence of a
    transition, so a row whose cells did not move cannot have moved either — and
    walking the rest would be a drift scan, which is not what this is.
    """
    rows = campaign_mod.read_state(member, label)
    members = campaign_mod.read_members(superstudy, label)
    changed = {}
    for row in members:
        if (
            row.get("study") != name
            or row.get("source_dataset") != advance.source_dataset
        ):
            continue
        before = row.get("lifecycle", "")
        after = source_lifecycle(rows, row["source_dataset"])
        if after != before:
            row["lifecycle"] = after
            changed[row["source_dataset"]] = (before, after)
    if changed:
        campaign_mod.write_members(superstudy, label, members)
    return changed


def member_message(name, label, advance, changed):
    """The super's save message for one cell-transition in one member.

    A lifecycle change is the subject when there is one — it is the rarest and most
    consequential thing a cell-transition does to a member, and the line a reader with
    git but not the cluster is scanning for; the transition that caused it is then the
    body. Otherwise the subject is the transition itself, naming the cell. Never a
    bare count, which leaves the super's own history unreadable.
    """
    if not changed:
        return f"mechababs iterate: {name} {advance.transition} {advance.cell} (campaign {label!r})"
    moves = ", ".join(
        f"{source} is now {after}" for source, (_, after) in sorted(changed.items())
    )
    body = [f"{advance.transition}  {advance.cell}"]
    body += [
        f"lifecycle: {source}  {before or '-'} -> {after}"
        for source, (before, after) in sorted(changed.items())
    ]
    return f"mechababs iterate: {name} {moves} (campaign {label!r})\n\n" + "\n".join(
        body
    )


def describe_counts(status):
    """The live babs counts, in one readable clause."""
    return (
        f"{status['total']} job(s): {status['submitted']} submitted, "
        f"{status['done']} done, {status['failed']} failed"
    )


def advance_cell(study, label, rows, row, *, dry_run=False):
    """Advance one cell by at most one transition. The transition's name, or ``None``.

    Returns ``None`` for every state that costs nothing — done, waiting, jobs still in
    flight, jobs failed — which is also what keeps those cells from consuming
    ``--batch``. The name rather than a bool, because the super's save message has
    to say what happened (``member_message``).
    """
    state, detail = route(rows, row)
    where = cell_label(row)
    source_dataset, app_config = campaign_mod.cell_key(row)

    if state == DONE:
        note(f"{where}: merged — nothing to do")
        return None

    if state == WAITING:
        note(f"{where}: waiting on {detail} — passed over")
        return None

    if state == SCAFFOLD:
        note(f"{where}: not started -> scaffold")
        dispatch.scaffold(study, label, source_dataset, app_config, dry_run=dry_run)
        return SCAFFOLDED

    # ACTIVE: the one state whose next step is not knowable from the shard. Ask babs,
    # every iterate, rather than mirroring a job status into a column that could drift.
    status = babs_status.read_status(Path(study) / detail)
    action = babs_status.decide(status)
    note(f"{where}: {describe_counts(status)} -> {action}")

    if action == "skip":
        return None
    if action == "fail":
        # Loud, and stopping at this cell only: the campaign keeps reconciling, and a
        # human decides what happened here. Nothing is written — the next iterate
        # re-derives this from the same live counts.
        note(
            f"!! {where}: {status['failed']} job(s) FAILED — NOT merging. "
            f"Look at it with:  babs status {detail}"
        )
        return None
    if action == "submit":
        dispatch.submit(study, label, source_dataset, app_config, dry_run=dry_run)
        return SUBMITTED

    dispatch.merge(study, label, source_dataset, app_config, dry_run=dry_run)
    return MERGED


def work_list(rows, app=None):
    """The cells this iterate will consider, in shard order, as ``(source, app)`` keys.

    Row order is the ordering mechanism — there is no priority scheme — so this
    preserves it. ``app`` narrows to one app config's cells by its stem; naming
    a stem the campaign does not have is a typo far more often than it is an empty
    campaign, so it is refused rather than reported as "nothing to do".
    """
    keys = [campaign_mod.cell_key(row) for row in rows]
    if app is None:
        return keys
    matched = [
        key for key, row in zip(keys, rows) if scaffold_mod.app_stem(key[1]) == app
    ]
    if not matched:
        stems = sorted({scaffold_mod.app_stem(key[1]) for key in keys})
        sys.exit(
            f"no cells for --app {app!r} in this campaign.\n"
            f"This campaign's apps are: {', '.join(stems) or '(none)'}"
        )
    return matched


def record(superstudy, label, name, member, advance, *, dry_run=False):
    """Register one advance at the super: one commit, for one cell.

    A study-only campaign needs none of this — the transition's own ``datalad run``
    commits in the study, which IS the operating level. With a super above it, that
    same run leaves the member's gitlink advanced and only the super can register it:
    the gitlink, plus the catalog row when the member's lifecycle changed. Only the
    transitions in ``RECORDED`` get here; the others move nothing in git.

    It runs before the next cell is attempted, for two reasons. A failure mid-member
    then leaves the super exactly as far as the last success, rather than holding a
    stale gitlink it would later blame on an intervention nobody made. And it keeps
    the grain the same at every level: the member's own history is already one
    ``datalad run`` per advancing cell, and a super commit spanning two derivatives
    would mix subdatasets whose provenance has nothing to do with each other.
    """
    if dry_run:
        # Nothing moved, so the shard still reads as it did and the lifecycle cannot
        # be computed from it. Say what would be recorded and leave it there.
        note(f"DRY-RUN: {name} would record {advance.transition} {advance.cell}")
        return
    changed = update_lifecycle(superstudy, label, name, member, advance)
    paths = [member]
    if changed:
        paths.append(campaign_mod.members_path(superstudy, label))
    utils.save_paths(superstudy, paths, member_message(name, label, advance, changed))


def member_studies(superstudy, label, target=None):
    """The member studies to advance, in catalog order, de-duplicated.

    Catalog order is the ordering interface at the super, the way row order is
    within a shard: several source datasets in one member give several catalog
    rows, and the member is advanced once. ``target`` narrows to one member and is
    matched against the catalog rather than the filesystem, so naming a directory
    that exists but was never selected into this campaign is an error rather than
    a silent no-op.
    """
    rows = campaign_mod.read_members(superstudy, label)
    names = list(dict.fromkeys(r["study"] for r in rows if r.get("study")))
    if target is None:
        return names
    wanted = Path(target).name
    if wanted not in names:
        sys.exit(
            f"{target} is not a member of campaign {label!r}.\n"
            f"Members: {', '.join(names) if names else '(none selected yet)'}"
        )
    return [wanted]


def run_iterate(root=".", *, batch=None, app=None, study=None, dry_run=False):
    """Resolve where we are standing, then advance cells, one cell-transition at a time.

    ``require_selected_campaign`` gates advancing on standing at the configured level;
    the inner verbs carry no such check, so a ``datalad rerun`` of one keeps working
    wherever it lands.

    The loop is the same at both levels: for each study to advance, clean in, then
    walk its cells in shard order and advance each by at most one transition. At a
    study there is one study to advance, and the transition's own ``datalad run`` is
    its record. At a superstudy the studies are the **installed** members in catalog
    order, and every cell-transition is recorded at the super before the next cell is
    attempted (see ``record``). A member the user has pushed and uninstalled is
    skipped with a note rather than reinstalled, including when ``study`` names it
    directly — reclaiming space is a decision an ``iterate`` must not quietly reverse.

    Where you stand gives the *level*; ``study`` narrows *within* it. ``batch`` is
    the budget for the whole ``iterate``, spent in catalog order and then shard order,
    so ``--batch 5`` advances the five most important cells in the superstudy,
    wherever they live: catalog order is a priority interface, not only an ordering.

    Returns the ``Advance`` records, in the order they happened.
    """
    selected = campaign_mod.require_selected_campaign(root)
    root, label = selected.root, selected.label

    # The single writer, in exactly one place, at the level the campaign is OPERATED
    # from. One lock covers the whole iterate: a per-member lock would hold nothing
    # over the super's own writes (the gitlink and the catalog row), which are what a
    # second iterate would collide with. Not inside the verbs: an flock is per
    # open-file-description, so a lock taken in a dispatched verb would deadlock
    # against this one.
    with utils.flocked(campaign_mod.flock_path(selected.operated_at)):
        at_super = campaign_mod.is_superstudy_campaign(root, label)
        if at_super:
            members = member_studies(root, label, study)
            note(f"superstudy iterate over {len(members)} member(s) in {root}")
            # The super's OWN tree only. Each member is checked separately, right
            # before it is advanced, so one member's drift stops that member rather
            # than the whole fan-out.
            require_clean_shallow(root, what="a superstudy iterate", ignore=members)
            studies = [Path(root) / name for name in members]
        else:
            if study:
                sys.exit(
                    f"campaign {label!r} here is configured at a study, so there are "
                    f"no members to select between.\n--study narrows a superstudy iterate."
                )
            studies = [Path(root)]

        budget = batch
        advanced = []
        for i, study_path in enumerate(studies):
            # The budget is checked before a study is even looked at: an iterate with
            # nothing left to spend must not touch the filesystem to discover that.
            if budget is not None and budget <= 0:
                note(
                    f"--batch {batch} reached — {len(studies) - i} member(s) left for "
                    f"the next iterate"
                )
                break
            if at_super:
                # Never reinstalled to advance it: reinstall it later and its shard
                # drives it, state derived from ground truth as always.
                if not study_mod.is_study_root(study_path):
                    note(f"{study_path.name}: not installed — left alone")
                    continue
                # The one thing only the super can see: whether its gitlink still
                # matches the member's HEAD. Scoped to this member, so it is flat
                # where a whole-super status would be linear in members.
                utils.require_clean_gitlink(root, study_path.name)
            campaign_mod.require_statefile(study_path, label)
            # Once per study, before any of its cells: `datalad run --explicit`
            # captures only what a verb declares, so a run recorded on top of
            # uncommitted work would not describe the tree it ran in.
            require_clean_shallow(study_path, what="an iterate")

            cells = work_list(campaign_mod.read_state(study_path, label), app)
            scope = f" ({app})" if app else ""
            note(f"iterate over {len(cells)} cell(s) in {study_path}{scope}")
            for j, key in enumerate(cells):
                if budget is not None and budget <= 0:
                    note(
                        f"--batch {batch} reached — {len(cells) - j} cell(s) left for "
                        f"the next iterate"
                    )
                    break
                # Re-read: the verbs write the shard themselves, so a copy taken
                # before the loop is stale the moment a cell advances.
                rows = campaign_mod.read_state(study_path, label)
                row = campaign_mod.find_cell(rows, *key)
                transition = advance_cell(study_path, label, rows, row, dry_run=dry_run)
                if not transition:
                    continue
                advance = Advance(transition, cell_label(row), key[0])
                advanced.append(advance)
                if budget is not None:
                    budget -= 1
                if at_super and transition in RECORDED:
                    record(
                        root,
                        label,
                        study_path.name,
                        study_path,
                        advance,
                        dry_run=dry_run,
                    )

        if dry_run:
            note(
                f"DRY-RUN: {len(advanced)} cell(s) would advance. Nothing changed, so "
                f"no cell's state moved — a real iterate may advance more."
            )
        else:
            note(f"iterate done: {len(advanced)} cell(s) advanced.")
        return advanced
