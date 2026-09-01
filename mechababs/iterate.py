"""iterate.py — the reconciler tick.

Every verb in a cell's life exists on its own (scaffold, submit, merge), and each one
refuses a cell that is not in the state it advances from. ``iterate`` is what decides
*which* verb a cell is owed, and dispatches it: one tick reads the statefile shard,
advances each cell by **at most one transition**, and stops.

**Level-triggered, not edge-triggered.** A tick never remembers what the last one did.
It re-reads ground truth — the shard's columns, plus a live ``babs status`` for any
cell that is running — and re-derives what each cell needs from that alone. So a
crashed tick, a hand-edited shard, or a cell repaired by hand between ticks all
converge on the next pass instead of accumulating drift. This is also why there is no
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
not a halt: the tick says so and goes on to the next cell. The next tick re-checks.

**A failure is flagged, never merged, and never persisted.** When the live counts say
jobs failed, the cell is marked loudly and left alone — merging a partial set would
quietly produce a derivative that looks complete. The flag is this tick's reading, not
a column, so a repair-and-resubmit (docs/interventions.md) takes effect with nothing
to clear.

**iterate is a plain coordinator.** It is not itself wrapped in a ``datalad run`` — it
dispatches one run per advancing cell, so runs never nest at the same level — and it
writes no statefile columns: the verbs do that, inside their own runs. The single
writer is enforced by the campaign flock, taken in exactly one place: around the whole
tick, at the level the campaign is operated from.
"""

import sys
from pathlib import Path

from mechababs import babs_status, dispatch
from mechababs import campaign as campaign_mod
from mechababs import scaffold as scaffold_mod
from mechababs import study as study_mod
from mechababs import utils
from mechababs.utils import require_clean_shallow

# Every line iterate writes about its own reasoning carries this. A tick interleaves
# mechababs' decisions with datalad's, babs's and git's output, and the old reconciler's
# were indistinguishable from the noise around them — so the prefix is here from the
# start rather than retrofitted. The `+ <command>` echoes from the verbs are left
# unprefixed on purpose: those are commands, not commentary.
PREFIX = "mechababs>"

# The four cell states, read off the shard's columns. There is no status enum in the
# statefile; these are names for what the columns already say.
DONE = "done"
ACTIVE = "active"
WAITING = "waiting"
SCAFFOLD = "scaffold"


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
        # hand-edited shard. One broken cell must not take the tick down: say what is
        # wrong and let the others advance.
        return WAITING, f"{stem} (no cell for it in this shard)"
    if not producer.get("merged"):
        return WAITING, stem
    return SCAFFOLD, ""


def describe_counts(status):
    """The live babs counts, in one readable clause."""
    return (
        f"{status['total']} job(s): {status['submitted']} submitted, "
        f"{status['done']} done, {status['failed']} failed"
    )


def advance_cell(study, label, rows, row, *, dry_run=False):
    """Advance one cell by at most one transition. True if something was dispatched.

    Returns False for every state that costs nothing — done, waiting, jobs still in
    flight, jobs failed — which is also what keeps those cells from consuming
    ``--batch``.
    """
    state, detail = route(rows, row)
    where = cell_label(row)
    source_dataset, app_config = campaign_mod.cell_key(row)

    if state == DONE:
        note(f"{where}: merged — nothing to do")
        return False

    if state == WAITING:
        note(f"{where}: waiting on {detail} — not scaffolded this tick")
        return False

    if state == SCAFFOLD:
        note(f"{where}: not started -> scaffold")
        dispatch.scaffold(study, label, source_dataset, app_config, dry_run=dry_run)
        return True

    # ACTIVE: the one state whose next step is not knowable from the shard. Ask babs,
    # every tick, rather than mirroring a job status into a column that could drift.
    status = babs_status.read_status(Path(study) / detail)
    action = babs_status.decide(status)
    note(f"{where}: {describe_counts(status)} -> {action}")

    if action == "skip":
        return False
    if action == "fail":
        # Loud, and stopping at this cell only: the campaign keeps reconciling, and a
        # human decides what happened here. Nothing is written — the next tick
        # re-derives this from the same live counts.
        note(
            f"!! {where}: {status['failed']} job(s) FAILED — NOT merging. "
            f"Look at it with:  babs status {detail}"
        )
        return False
    if action == "submit":
        dispatch.submit(study, label, source_dataset, app_config, dry_run=dry_run)
        return True

    dispatch.merge(study, label, source_dataset, app_config, dry_run=dry_run)
    return True


def work_list(rows, app=None):
    """The cells this tick will consider, in shard order, as ``(source, app)`` keys.

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


def tick(study, label, *, batch=None, app=None, dry_run=False):
    """One reconciler tick over ``study``'s shard for ``label``. Returns cells advanced.

    ``study`` is a parameter, never the cwd: at a superstudy the reconciler will stand
    at the super and drive member studies, so nothing here may assume it is standing
    in the study it is reconciling.

    The single-writer flock is held by ``run_iterate``, not here — a fan-out calls
    this once per member, and taking the lock per call would release it between
    members and leave exactly the gaps it exists to close.
    """
    study = Path(study)
    campaign_mod.require_statefile(study, label)

    # Once per tick, at the study being reconciled. `datalad run --explicit` captures
    # only what a verb declares, so anything already uncommitted here did not come from
    # mechababs — and a run recorded on top of it would not describe the tree it
    # ran in. Cheap: a gitlink compare, no descent into submodule worktrees.
    require_clean_shallow(study, what="an iterate tick")

    cells = work_list(campaign_mod.read_state(study, label), app)
    scope = f" ({app})" if app else ""
    note(f"tick over {len(cells)} cell(s) in {study}{scope}")

    advanced = 0
    for i, key in enumerate(cells):
        if batch is not None and advanced >= batch:
            note(
                f"--batch {batch} reached — {len(cells) - i} cell(s) left for "
                f"the next tick"
            )
            break
        # Re-read: the verbs write the shard themselves, so a copy taken at tick
        # start is stale the moment a cell advances. Ground truth, every cell.
        rows = campaign_mod.read_state(study, label)
        row = campaign_mod.find_cell(rows, *key)
        if advance_cell(study, label, rows, row, dry_run=dry_run):
            advanced += 1

    if dry_run:
        note(
            f"DRY-RUN: {advanced} cell(s) would advance. Nothing changed, so no "
            f"cell's state moved — a real tick may advance more."
        )
    else:
        note(f"tick done: {advanced} cell(s) advanced.")
    return advanced


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
    """Resolve where we are standing, then tick — once, or once per member.

    The **configured-level check lives here**, on the user-driven path, and not on
    ``mechababs-inner``: advancing a campaign is gated on standing at the level it was
    configured, while reproducing a recorded run (a ``datalad rerun`` of an inner verb)
    must keep working wherever it lands. ``require_selected_campaign`` is that check —
    a study root, a campaign selected by ``MECHABABS_CAMPAIGN``, this process running
    that campaign's own venv, and the campaign not belonging to a level above.

    Where you stand gives the *level*; ``study`` narrows *within* it. At a superstudy
    the tick is per member, and ``batch`` caps the **whole tick** rather than each
    member: the budget is spent in catalog order and the fan-out stops when it runs
    out. That is what makes catalog order a priority interface rather than only an
    ordering — ``--batch 5`` advances the five most important transitions in the
    superstudy, wherever they live — and it keeps ``--batch N`` meaning one thing at
    either level: at most N cells advance, per tick, full stop.

    Only **installed** members are advanced. A member the user has pushed and
    uninstalled is skipped with a note rather than reinstalled, including when
    ``study`` names it directly — reclaiming space is a decision a tick must not
    quietly reverse.
    """
    selected = campaign_mod.require_selected_campaign(root)
    root, label = selected.root, selected.label

    # The single writer, in exactly one place — and at the level the campaign is
    # OPERATED from, which for a member of a super-campaign is not the study whose
    # shard is being advanced. One lock covers the whole tick: a fan-out that took
    # each member's lock in turn would hold nothing between members, and nothing at
    # all over the super's own writes (the gitlink and the catalog row), which are
    # the writes a second tick would actually collide with.
    #
    # Not in `dispatch` and not in the verbs: an flock is per open-file-description,
    # so a lock taken inside a verb this tick dispatches would deadlock against the
    # one held here.
    with utils.flocked(campaign_mod.flock_path(selected.operated_at, label)):
        if not campaign_mod.is_superstudy_campaign(root, label):
            if study:
                sys.exit(
                    f"campaign {label!r} here is configured at a study, so there are "
                    f"no members to select between.\n--study narrows a superstudy tick."
                )
            return tick(root, label, batch=batch, app=app, dry_run=dry_run)

        members = member_studies(root, label, study)
        note(f"superstudy tick over {len(members)} member(s) in {root}")

        # Clean in at the super, once, before any member is touched — but only the
        # super's OWN tree: its campaign dir, its catalog, anything stray at its root.
        # The members are excluded because each is asked about separately, immediately
        # before it is advanced, so nothing is checked twice and one member's drift stops
        # that member rather than the whole fan-out. What is left here is the dirt only
        # this level can see, and the same contract `tick` applies one level down:
        # anything uncommitted is not mechababs' and must not be committed as ours.
        require_clean_shallow(root, what="a superstudy iterate tick", ignore=members)

        advanced = 0
        remaining = batch
        for i, name in enumerate(members):
            # The batch is the tick's budget, not each member's, and it is spent in
            # catalog order — so the catalog decides who gets the budget, not just who
            # goes first. Checked before the member is even looked at: a tick with
            # nothing left to spend must not touch the filesystem to discover that.
            if remaining is not None and remaining <= 0:
                note(
                    f"--batch {batch} reached — {len(members) - i} member(s) left for "
                    f"the next tick"
                )
                break
            member = Path(root) / name
            # A member that has left the cluster is left alone — never reinstalled to
            # advance it. The user's uninstall IS the signal: a study whose derivatives
            # are pushed and whose content is dropped has nothing here to advance, and
            # reinstalling to find that out would undo the space they just reclaimed.
            # Reinstall it later and its shard drives it, state derived as always from
            # ground truth, so no "done" marker is needed for this to be safe.
            if not study_mod.is_study_root(member):
                note(f"{name}: not installed — left alone")
                continue
            # Clean in, before this member is touched — and scoped to it. The member's
            # own tree is covered by `tick`, and again before each transition it
            # dispatches, so what is left for the super is the one thing only the super
            # can see: whether its gitlink still matches the member's HEAD. A whole-super
            # status would answer this for every member at a cost linear in members, and
            # would answer it too early to be worth much; this one is flat and current.
            utils.require_clean_gitlink(root, name)
            moved = tick(member, label, batch=remaining, app=app, dry_run=dry_run)
            advanced += moved
            if remaining is not None:
                remaining -= moved
            # Then record. A study-only campaign needs none of this: the transition's own
            # `datalad run` commits in the study, which IS the operating level. With a
            # super above it, that same run leaves the member's gitlink advanced and only
            # the super can register it — so each member is recorded as it lands, rather
            # than at the end, so an interrupted fan-out still leaves the super
            # describing the members that did advance.
            if moved and not dry_run:
                utils.save_paths(
                    root,
                    member,
                    f"mechababs iterate: {name} advanced {moved} cell(s) "
                    f"in campaign {label!r}",
                )
        return advanced
