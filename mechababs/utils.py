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
    ds = Dataset(str(root))
    if isinstance(paths, (str, Path)):
        paths = [paths]
    paths = [str(p) for p in paths]
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

    pending = PendingSave()
    yield pending
    if not pending.message:
        raise RuntimeError(f"campaign_save_scope({paths}) exited with no message set")

    results = ds.save(
        path=paths,
        message=pending.message,
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


def shallow_status(root):
    """Porcelain status of ``root`` WITHOUT descending into submodule worktrees.

    ``--ignore-submodules=dirty`` is the whole point: git still compares each
    submodule's recorded commit against its HEAD (a gitlink compare — one ref read
    per submodule) but does not walk its working tree. That walk is what makes a
    status over a study with real source data expensive, and it is never what this
    check is looking for.
    """
    out = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--ignore-submodules=dirty"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def require_clean_shallow(root, *, what="this operation"):
    """Refuse unless ``root`` is clean at its own level. Cheap enough for a tick.

    The backstop for `datalad run --explicit`. Explicit mode captures ONLY the
    declared outputs, which is what keeps a run from deep-walking `sourcedata/raw`
    — but it also means a stray side-write next to them is silently left behind
    rather than swept into the commit. So the tree is checked once, loudly, before
    dispatching: anything already uncommitted here did not come from mechababs, and
    a run that starts on top of it produces a record that does not describe the
    tree it ran in.

    Deliberately shallow (see ``shallow_status``): a dirty submodule *worktree* is
    not this check's business, a moved submodule *pointer* is.
    """
    dirty = shallow_status(root)
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
