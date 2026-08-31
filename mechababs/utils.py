"""utils.py — the campaign's datalad primitives: `locked` and `datalad_save_scope`.

The two things every campaign mutator (`add-dataset`, `iterate`,
`retire-derivative`) needs: a single-writer lock, and a way to collapse a block of
work into one labeled provenance node. They are kept together because they are
always used together — take the lock, then open a scope.

`datalad_save_scope` is a clean-in / one-node-out context manager built entirely
on `ds.save(since=<entry>)` (datalad#7821's run-merge engine, exposed via `since=`).
It records HEAD at entry and, on clean exit, collapses everything the block did
into ONE first-parent commit — one labeled "node" — on `ds`'s mainline:

  - a block that only edited files              -> a flat commit;
  - a block that made inner commits (babs init) -> a merge whose FIRST parent is
                                                   the entry sha and whose second
                                                   parent is the inner chain, so
                                                   `git log --first-parent` shows
                                                   just this one labeled step (the
                                                   inner commits stay reachable +
                                                   `datalad rerun`-able off it).

"One node" is PER touched dataset, not one commit total. `recursive=True` is the
load-bearing correctness knob: without it a subdataset-deep change (the derivative's
own commits under a study under the campaign) is a silent `notneeded` no-op, so the
campaign never records the advance. With it, one call bumps a gitlink up each level
of the nest — one clean node at the derivative, one at the study, one at the campaign
(the irreducible per-level ripple: git can't move the super's pointer without the
sub having a new commit to point at).

The clean-in guard makes "everything since base == this block's work" true, so the
node is attributable; a dirty tree raises rather than absorbing unrelated changes.
"""

import fcntl
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from datalad.api import Dataset

from mechababs.state import LOCK_FILENAME


def run(*cmd, **kwargs):
    """Run a command, echoing it; abort on non-zero exit."""
    print("+ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def describe_result(result):
    """A datalad result record's own explanation, in one line.

    ``message`` is a plain string, a lazy ``(format, *args)`` tuple, or absent — so
    a naive f-string prints a tuple at the user when it matters most.
    """
    message = result.get("message") or result.get("action", "no detail")
    if isinstance(message, tuple):
        message = message[0] % message[1:]
    return str(message)


class PendingSave:
    """The save a ``campaign_save_scope`` block is working toward.

    The block sets ``message``: a useful label names what the block did (the apps it
    staged, the cell it advanced), which is not knowable at entry — and entry is
    where the clean-in check has to happen.
    """

    def __init__(self):
        self.message = None


@contextmanager
def campaign_save_scope(root, paths):
    """Clean in, one commit out: whatever the block writes at ``paths``, committed.

    ``paths`` is one path or several, and the caller **declares everything it
    changed** — the same declare-your-outputs contract as the run wrapper. Nothing
    outside the declaration is evaluated, in either the check or the save.

    A declared path that is a subdataset is **gitlink-registered, never recursed**
    (``eval_submodule_state="commit"``): the super's record of a member is which
    commit it points at, and descending into the member's worktree would both cost
    a walk and pull that member's own uncommitted work into a commit at this level.
    Stray content inside a subdataset is the once-per-tick shallow check's to catch.

    **Clean in.** ``path`` must be clean *before* the block writes, so the commit is
    attributable — everything in it is this block's work, and no pre-existing edit is
    silently absorbed into a mechababs-authored commit. ``campaign init`` passes it
    trivially (its target does not exist yet); the guard matters for the callers that
    write into a directory a human may have touched — ``add-dataset`` saving the
    statefile, scaffold pinning an inclusion.

    The check is **path-scoped**, which is also what makes it cheap: a campaign dir
    holds no subdatasets, so this is a status over a handful of small files rather
    than a walk of the study's sourcedata.

    Files land in git rather than annex, but that is the campaign's own
    ``.gitattributes`` (written at init) doing it, not a flag on this save.

    Through ``datalad.api``, not a shelled-out ``datalad``: datalad is a declared
    dependency, so it is importable wherever this runs — including the ``uvx``
    install, which has no ``bin/datalad`` beside the interpreter to find. (Sibling to
    ``datalad_save_scope`` below, whose clean-in is whole-dataset and whose save is
    ``since=``-based; here the scope is one directory, so both can be path-scoped.)
    """
    paths = require_clean_paths(root, paths)
    pending = PendingSave()
    yield pending
    if not pending.message:
        raise RuntimeError(f"campaign_save_scope({paths}) exited with no message set")
    save_paths(root, paths, pending.message)


def _declared(paths):
    if isinstance(paths, (str, Path)):
        paths = [paths]
    return [str(p) for p in paths]


def require_clean_paths(root, paths):
    """Exit unless every declared path is clean. Returns them normalised.

    The clean-in half, split out because not every writer wants it wrapped *around*
    its work. ``iterate`` at a superstudy checks the super once at the top of the
    tick — before any member is touched — and then records each member as it
    advances, so its check and its saves are separated by the actions they bracket.
    """
    ds = Dataset(str(root))
    paths = _declared(paths)
    dirty = ds.status(
        path=paths,
        untracked="all",
        eval_subdataset_state="commit",
        result_renderer="disabled",
        on_failure="ignore",
        return_type="list",
    )
    dirty = [r for r in dirty if r.get("state") != "clean"]
    if dirty:
        sys.exit(
            f"refusing to write into {', '.join(paths)}: it is not clean, and the "
            f"commit would absorb changes mechababs did not make.\n"
            + "\n".join(f"  {r.get('state')}: {r.get('path')}" for r in dirty)
        )
    return paths


def save_paths(root, paths, message):
    """Commit exactly the declared paths at ``root``. No clean-in check of its own.

    The save half. Its caller has already established that what it commits is its
    own work — either by a clean-in wrapped around the block
    (``campaign_save_scope``) or by one taken before the actions being recorded.
    """
    ds = Dataset(str(root))
    paths = _declared(paths)
    results = ds.save(
        path=paths,
        message=message,
        result_renderer="disabled",
        on_failure="ignore",
        return_type="list",
    )
    failed = [r for r in results if r.get("status") not in ("ok", "notneeded")]
    if failed:
        sys.exit(
            f"failed to commit {', '.join(paths)} into {root}\n"
            + "\n".join(
                f"  {r.get('status')}: {r.get('path')} ({describe_result(r)})"
                for r in failed
            )
        )


def shallow_status(root, *paths):
    """Porcelain status of ``root`` WITHOUT descending into submodule worktrees.

    ``--ignore-submodules=dirty`` is the whole point: git still compares each
    submodule's recorded commit against its HEAD (a gitlink compare — one ref read
    per submodule) but does not walk its working tree. That walk is what makes a
    status over a study with real source data expensive, and it is never what this
    check is looking for.

    ``paths`` narrows it to a pathspec, which is what keeps the check flat at a
    superstudy. Unscoped, the cost is linear in members — git stats every member
    directory whether or not their gitlinks are compared (measured: 23 ms over 200
    members, 1.7 ms over one; ``--ignore-submodules=all`` saves nothing, so the
    cost is the directory scan, not the comparison). Scoped to a single member it
    is 2.7 ms no matter how large the superstudy is.
    """
    cmd = ["git", "-C", str(root), "status", "--porcelain", "--ignore-submodules=dirty"]
    if paths:
        cmd += ["--", *(str(p) for p in paths)]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def require_clean_gitlink(root, member):
    """Refuse unless ``root``'s recorded pointer to ``member`` is up to date.

    The superstudy's whole stake in a member it is about to advance. The member's
    own tree is not this check's business — ``tick`` checks it there, and again
    before each transition it dispatches. What only the super can see is whether its
    gitlink still matches the member's HEAD, and a stale one matters because the
    follow-up save would then commit somebody else's advance as ours.

    This is the **only** check of a member's gitlink: the super's own once-per-tick
    check ignores the members precisely so each is asked about once, here, right
    before it is touched. It costs the same in a superstudy of a thousand as in one
    of two.

    A stale gitlink **stops the tick** rather than skipping the member. A member
    moving underneath us is a bug or an intervention, not a condition to reconcile
    past — unlike a failed cell, which is a known outcome the reconciler notes and
    works around.
    """
    rel = Path(member).relative_to(root) if Path(member).is_absolute() else Path(member)
    dirty = shallow_status(root, rel)
    if dirty:
        sys.exit(
            f"{rel} has moved since {root} last recorded it, and mechababs did "
            f"not move it.\n" + "\n".join(f"  {line}" for line in dirty) + "\n"
            "Commit or reset it at the superstudy, then run again — otherwise "
            "this tick would record that advance as its own."
        )


def require_clean_shallow(root, *, what="this operation", ignore=()):
    """Refuse unless ``root`` is clean at its own level. Cheap enough for a tick.

    The backstop for `datalad run --explicit`, and it is the *only* one: explicit
    mode does not check the dataset at all (verified — plain `datalad run` refuses a
    dirty dataset, `--explicit` runs and commits just its declared outputs, leaving
    the stray file behind). That is the trade explicit mode makes to avoid
    deep-walking `sourcedata/raw`, so a stray side-write is silently left rather than
    swept into the commit. Hence this, loudly, before dispatching: anything already
    uncommitted here did not come from mechababs, and a run recorded on top of it
    would not describe the tree it ran in.

    ``ignore`` names paths whose state is somebody else's to check — at a superstudy,
    the members, each checked by ``require_clean_gitlink`` immediately before it is
    advanced. Excluded by git pathspec rather than by filtering the output, so a path
    is never matched by string-comparing against git's own quoting. What is left is
    the level's *own* tree: its campaign dir, its catalog, anything stray at its root.

    Deliberately shallow (see ``shallow_status``): a dirty submodule *worktree* is
    not this check's business, a moved submodule *pointer* is.
    """
    paths = [".", *(f":(exclude){p}" for p in ignore)] if ignore else ()
    dirty = shallow_status(root, *paths)
    if dirty:
        raise RuntimeError(
            f"{root} is not clean — refusing {what}.\n"
            "Uncommitted work here is not mechababs', and a run recorded on top "
            "of it would not describe the tree it ran in. Commit or discard it "
            "first:\n" + "\n".join(f"  {line}" for line in dirty)
        )


@contextmanager
def flocked(lock):
    """Hold an exclusive flock on ``lock`` (created if absent) for the block."""
    with open(lock, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def locked(campaign):
    """Hold the campaign's single-writer flock around a read-modify-write.

    The campaign-level primitive that keeps `add-dataset`, `iterate`, and
    `retire-derivative` from interleaving: each holds this for the whole
    read-modify-write of the ledger and the nest it describes.
    """
    with flocked(Path(campaign) / LOCK_FILENAME):
        yield


@contextmanager
def datalad_save_scope(ds, message, *, recursive=False, dry_run=False, **save_kwargs):
    """Group everything done in the block into ONE labeled node at `ds`.

    dry_run yields the block (whose own steps print rather than mutate), then skips
    the clean-in guard and the save, printing the save that would have run — so a
    caller uses one code path for real and dry runs.

    `since=` is helper-owned (it collides with the entry sha); everything else
    `ds.save()` accepts (`path=`, `jobs=`, `to_git=`, …) rides `**save_kwargs`.
    """
    if dry_run:
        yield ds
        rflag = "--recursive " if recursive else ""
        print(
            f"DRY-RUN  datalad save --dataset {ds.path} --since <HEAD> "
            f"{rflag}--message {message!r}",
            file=sys.stderr,
        )
        return
    if ds.repo.dirty:
        raise RuntimeError(f"{ds.path} is dirty; refusing to open datalad_save_scope")
    base = ds.repo.get_hexsha()
    yield ds
    ds.save(since=base, message=message, recursive=recursive, **save_kwargs)
