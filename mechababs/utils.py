"""utils.py — the primitives every mechababs writer shares: the flock, and saving.

Two things, and each verb reaches for both: a single-writer lock over the campaign,
and a way to land a block of work as one attributable commit at one level.

**Saving is always path-scoped and always declared** (``campaign_save_scope``), and
a declared subdataset is gitlink-registered, never recursed. That is what makes
"every level commits its own facts" implementable: a verb that changes a member and
its superstudy opens one scope per level, nested outer-first, and each commits only
what that level can see.

**Checking is deliberately shallow** (``shallow_status``): a dirty submodule
*worktree* is never this layer's business, a moved submodule *pointer* always is.
"""

import fcntl
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from datalad.api import Dataset


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
    Stray content inside a subdataset is the once-per-iterate shallow check's to catch.

    **Clean in.** ``path`` must be clean *before* the block writes, so the commit is
    attributable — everything in it is this block's work, and no pre-existing edit is
    silently absorbed into a mechababs-authored commit. ``campaign init`` passes it
    trivially (its target does not exist yet); the guard matters for the callers that
    write into a directory a human may have touched — ``add-dataset`` saving the
    statefile, scaffold pinning an inclusion.

    The check is **path-scoped**, which is also what makes it cheap: a status over a
    handful of small files rather than a walk of the study's sourcedata. Files land
    in git rather than annex by the campaign's own ``.gitattributes``, not a flag here.

    Through ``datalad.api``, not a shelled-out ``datalad``: the ``uvx`` install has
    no ``bin/datalad`` beside the interpreter to find.
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
    iterate — before any member is touched — and then records each member as it
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

    What only the super can see is whether its gitlink still matches the member's
    HEAD; a stale one matters because the follow-up save would commit somebody
    else's advance as ours. The member's own tree is ``iterate``'s to check. This is
    the **only** check of a member's gitlink (the super's once-per-iterate check
    ignores the members), so it costs the same in a superstudy of a thousand as in
    one of two.

    A stale gitlink **stops the iterate** rather than skipping the member: a member
    moving underneath us is a bug or an intervention, not a condition to reconcile
    past.
    """
    rel = Path(member).relative_to(root) if Path(member).is_absolute() else Path(member)
    dirty = shallow_status(root, rel)
    if dirty:
        sys.exit(
            f"{rel} has moved since {root} last recorded it, and mechababs did "
            f"not move it.\n" + "\n".join(f"  {line}" for line in dirty) + "\n"
            "Commit or reset it at the superstudy, then run again — otherwise "
            "this iterate would record that advance as its own."
        )


def require_clean_shallow(root, *, what="this operation", ignore=()):
    """Refuse unless ``root`` is clean at its own level. Cheap enough for every iterate.

    The backstop for `datalad run --explicit`, and the *only* one: explicit mode
    does not check the dataset at all (it runs and commits just its declared outputs,
    leaving a stray file behind — the trade it makes to avoid deep-walking
    `sourcedata/raw`). So this, loudly, before dispatching: anything already
    uncommitted here did not come from mechababs, and a run recorded on top of it
    would not describe the tree it ran in.

    ``ignore`` names paths whose state is somebody else's to check — at a superstudy,
    the members, each checked by ``require_clean_gitlink`` right before it is
    advanced. Excluded by git pathspec rather than by filtering the output, so a path
    is never matched by string-comparing against git's own quoting.

    Deliberately shallow (see ``shallow_status``).
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
