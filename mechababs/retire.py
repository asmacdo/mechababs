"""retire.py — take a derivative out of its study, and reset the cell that made it.

Backs ``mechababs retire-derivative <derivative-path> (--remove | --path DEST)``.

A cell that has to be redone leaves a derivative that blocks the re-scaffold, since
``babs init`` refuses an existing path. The destination is a **required choice**:
``--path DEST`` keeps the evidence (logs, git history, run records) at a directory
outside the study, ``--remove`` deletes it. What the archive is and is not good for
is in docs/reference.md; in short, babs bakes absolute RIA paths in at init, so the
archive is evidence, not a resumable babs project.

**Resetting the cell is part of the same transition, not a follow-up.** Blanking the
shard's derived columns is what returns the cell to "not started" so the next
``iterate`` re-scaffolds it. Doing it inside the same scope means there is no window
where the derivative is gone but the reconciler still routes the cell as in-progress,
and no hand-edit to forget.

**Every level commits its own facts.** At a lone study that is one commit: the
deregistered subdataset, the ``.gitmodules`` entry, and the shard row. Under a
superstudy the member commits exactly those, and the superstudy commits the one
thing only it can see — the member's moved gitlink. Membership does not change: the
cell is still this campaign's, it is merely back at the start.

**A plain scoped save, not a ``datalad run``.** The change-making reconciler verbs
are dispatched so the study's history holds the verbatim, re-executable command.
Retire is not one of them, and recording it as re-executable would be a false
promise in both modes: ``--remove`` destroys content, so a rerun onto a
re-scaffolded cell would delete a *different* derivative, and ``--path`` names a
host directory outside every dataset, which does not resolve anywhere else. It is a
human intervention on a cell that went wrong — the same argument ``update-env``
makes about resolving against the live world — so it lands as a labeled save.
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from mechababs import campaign as campaign_mod
from mechababs import utils

DERIVATIVES_DIRNAME = "derivatives"
GITMODULES = ".gitmodules"


def parse_derivative_path(root, path):
    """``(study_rel, derivative_rel)`` for a derivative path, both root-relative.

    Accepts campaign-relative or absolute, because the path carries both halves of
    what is being retired and tab-completes. ``study_rel`` is ``.`` at a lone study
    and the member's path under a superstudy; ``derivative_rel`` is always
    ``derivatives/<name>``, which is exactly what the cell's ``babs`` column holds.
    """
    root = Path(root).resolve()
    given = Path(path)
    resolved = (given if given.is_absolute() else root / given).resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        sys.exit(
            f"{resolved} is not inside {root}.\n"
            "retire-derivative runs from the campaign root and names a derivative "
            "inside it."
        )
    parts = rel.parts
    if len(parts) < 2 or parts[-2] != DERIVATIVES_DIRNAME:
        sys.exit(
            f"not a derivative path: {path}\n"
            f"A derivative lives in a study's {DERIVATIVES_DIRNAME}/ — name it as "
            f"{DERIVATIVES_DIRNAME}/<name> at a study, or "
            f"<member>/{DERIVATIVES_DIRNAME}/<name> at a superstudy."
        )
    study_rel = Path(*parts[:-2]) if len(parts) > 2 else Path(".")
    return study_rel, f"{DERIVATIVES_DIRNAME}/{parts[-1]}"


def resolve_study(root, label, study_rel):
    """The study holding the derivative, with the configured-level rule enforced.

    Both directions, refused at the door the way ``add-dataset``'s member selector
    does it: a study-configured campaign has no member to name, and a super-configured
    one has no cells of its own. ``require_statefile`` then answers whether the study
    named actually carries this campaign's shard, which is the fact retire needs.
    """
    at_super = campaign_mod.is_superstudy_campaign(root, label)
    named_member = study_rel != Path(".")
    if named_member and not at_super:
        sys.exit(
            f"campaign {label!r} here is configured at a study, so a derivative "
            f"path names {DERIVATIVES_DIRNAME}/<name> directly.\n"
            f"Got a member-qualified path: {study_rel}/…"
        )
    if at_super and not named_member:
        sys.exit(
            f"campaign {label!r} here is configured at a superstudy, so a "
            f"derivative lives in one of its members.\n"
            f"Name it: <member>/{DERIVATIVES_DIRNAME}/<name>."
        )
    study = (Path(root) / study_rel).resolve()
    campaign_mod.require_statefile(study, label)
    return study


def find_cell(rows, derivative_rel):
    """The shard row whose ``babs`` column is this derivative, or exit saying so.

    The reverse of what scaffold recorded, and looked up rather than re-derived from
    the directory name: the ``babs`` column *is* the campaign's claim on this
    derivative, so matching it is what proves the cell being reset is the cell that
    made the thing being retired.
    """
    for row in rows:
        if row.get("babs") == derivative_rel:
            return row
    claimed = sorted(r["babs"] for r in rows if r.get("babs"))
    sys.exit(
        f"no cell in this campaign's statefile claims {derivative_rel}.\n"
        "Retiring resets the cell that produced the derivative, so a derivative "
        "no cell points at is not this campaign's to retire.\n"
        f"This campaign's scaffolded cells: {', '.join(claimed) or '(none)'}"
    )


def require_outside(dest, *insides):
    """Refuse a destination that resolves inside any of ``insides``.

    The rule that makes the archive safe to keep: a study — and a superstudy — is a
    published object we may not own, so a retired attempt parked inside one would
    travel with it to whoever clones it next. Checked on the *resolved* path, since
    DEST may not exist yet and may reach in through a symlink or a ``..``.
    """
    dest = Path(dest).expanduser().resolve()
    for inside in insides:
        inside = Path(inside).resolve()
        # `is_relative_to` is true of the directory itself, so DEST *being* the study
        # is caught here too — the case a plain prefix check would let through.
        if dest.is_relative_to(inside):
            sys.exit(
                f"--path must be outside the study, and {dest} is inside "
                f"{inside}.\n"
                "A study is a published object: a retired attempt kept inside it "
                "would travel with it to whoever clones it next. Name a directory "
                "outside, or use --remove if the evidence is not worth keeping."
            )
    return dest


def next_attempt(dest, prefix, name):
    """The first free ``DEST/<prefix>-<name>-attempt-<N>``. Never clobbers.

    ``prefix`` is the study's directory name, so one DEST can collect attempts from
    every study in a superstudy without two of them landing on the same path;
    ``attempt-N`` covers the same cell being retired more than once.
    """
    n = 1
    while (dest / f"{prefix}-{name}-attempt-{n}").exists():
        n += 1
    return dest / f"{prefix}-{name}-attempt-{n}"


def absorbed_gitdir(derivative):
    """Where ``derivative``'s real git directory lives, if it was absorbed. Else None.

    A subdataset's ``.git`` is normally a real directory, which is what makes the
    derivative self-contained on disk and its move a relocation rather than a copy.
    ``git submodule absorbgitdirs`` (and anything that calls it) replaces it with a
    ``gitdir:`` pointer into the parent's ``.git/modules/``, and a tree moved in that
    state is dead on arrival — the pointer resolves to nothing from the new location.
    """
    marker = Path(derivative) / ".git"
    if not marker.is_file():
        return None
    text = marker.read_text().strip()
    if not text.startswith("gitdir:"):
        sys.exit(f"{marker} is a file but not a gitdir pointer:\n{text}")
    target = Path(text[len("gitdir:") :].strip())
    return target if target.is_absolute() else (Path(derivative) / target).resolve()


def rehome_gitdir(derivative, gitdir):
    """Bring an absorbed git directory back inside the derivative, so it can travel.

    The inverse of ``git submodule absorbgitdirs``: move the real directory to
    ``<derivative>/.git`` and drop the ``core.worktree`` that pointed it back at the
    parent's tree, after which the derivative is an ordinary self-contained
    repository again.

    ``config --file`` rather than ``--git-dir``: the value being removed is precisely
    what makes git unable to open this repository, so anything that discovers the
    repo first fails before it can edit anything. Editing the config file directly is
    the only order that works.
    """
    marker = Path(derivative) / ".git"
    marker.unlink()
    shutil.move(str(gitdir), str(marker))
    subprocess.run(
        ["git", "config", "--file", str(marker / "config"), "--unset", "core.worktree"],
        check=True,
        capture_output=True,
    )


def rmtree(path):
    """Delete ``path``, including git-annex's deliberately read-only object store.

    git-annex takes the write bit off both its object files and the directories
    holding them — that is how it protects content from accidental modification, and
    it is why a plain ``shutil.rmtree`` of a derivative dies with ``EACCES`` on the
    first annexed object. Unlinking needs the write bit on the *containing
    directory* rather than on the file, so one pass restoring it to every directory
    is enough. Read-only directories are still listable and traversable, so the walk
    itself needs no help.

    Only ``--remove`` needs this. A move is a rename, which never touches the
    contents' permissions at all.
    """
    path = Path(path)
    for dirpath, _, _ in os.walk(path):
        os.chmod(dirpath, os.stat(dirpath).st_mode | stat.S_IWUSR | stat.S_IXUSR)
    shutil.rmtree(path)


def relocate(src, dest):
    """Move ``src`` to ``dest``, across filesystems if it comes to that.

    A rename when both sides are on one filesystem. Across filesystems (a cluster
    DEST on a different mount is the normal case) the tree is copied and the original
    deleted with :func:`rmtree`, not ``shutil.move``, whose cross-device fallback
    ends in a plain ``rmtree`` that dies on git-annex's read-only object store.

    ``symlinks=True`` keeps the annex's symlinks as symlinks rather than
    dereferencing them into copies of the content they point at.
    """
    src, dest = Path(src), Path(dest)
    try:
        os.rename(src, dest)
    except OSError:
        shutil.copytree(src, dest, symlinks=True)
        rmtree(src)


def drop_local_submodule_section(study, derivative_rel):
    """Drop git's stale ``submodule.<path>`` section from the study's LOCAL config.

    It is not committed and does not travel, so nothing about the study as published
    depends on this — but git leaves it behind when a submodule is deregistered, and
    a later ``git submodule`` call in that study reads it and is confused by a section
    naming a path that is gone.
    """
    subprocess.run(
        [
            "git",
            "-C",
            str(study),
            "config",
            "--remove-section",
            f"submodule.{derivative_rel}",
        ],
        check=False,
        capture_output=True,
    )


def detach(study, derivative_rel, dest):
    """Take the derivative out of the study's working tree; ``dest=None`` deletes it.

    Deliberately does **not** ``git rm`` the submodule first. datalad's save has a
    vanished-subdataset path that deregisters it — index *and* ``.gitmodules`` —
    once the directory is gone; pre-empting that with our own removal strips the
    index entry, so datalad's own ``git rm`` then fails on a pathspec that no longer
    matches. Making the directory vanish and letting the enclosing save do the
    bookkeeping is both simpler and the datalad-native path.
    """
    derivative = Path(study) / derivative_rel
    gitdir = absorbed_gitdir(derivative)
    if dest is None:
        rmtree(derivative)
        if gitdir is not None and gitdir.is_dir():
            # An absorbed git dir lives in the parent's `.git/modules/`, so removing
            # the worktree would leave the whole repository behind as cruft.
            rmtree(gitdir)
    else:
        if gitdir is not None:
            rehome_gitdir(derivative, gitdir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        relocate(derivative, dest)
    drop_local_submodule_section(study, derivative_rel)


def reset_cell(study, label, derivative_rel):
    """Blank the cell's derived columns, so ``route`` reads it as not started.

    Identity and topology are never touched: they are ``add-dataset``'s inputs, and
    the cell is not being unselected — it is going back to the start of its own life.
    """
    rows = campaign_mod.read_state(study, label)
    row = find_cell(rows, derivative_rel)
    for column in campaign_mod.DERIVED_COLUMNS:
        row[column] = ""
    campaign_mod.write_state(study, label, rows)
    return row


def require_claimed_cell(study, label, derivative_rel):
    """Prove a cell claims this derivative **before** anything is moved.

    The same lookup ``reset_cell`` does, run first for its refusal alone: raised
    inside the transition it would leave the derivative detached and the shard
    untouched, the half-retired state the single scope exists to prevent.
    """
    return find_cell(campaign_mod.read_state(study, label), derivative_rel)


def study_scope(study, label, derivative_rel):
    """What the study's retire commit covers, study-relative.

    Three things, and the run declares exactly these: the derivative that is leaving,
    the ``.gitmodules`` entry that registered it, and the shard row that claimed it.
    """
    return [
        derivative_rel,
        GITMODULES,
        str(campaign_mod.state_path(study, label).relative_to(study)),
    ]


def message(label, study_rel, derivative_rel, dest):
    """The commit subject: the command, and where the derivative went.

    The destination is named rather than elided. It is the only record of where the
    evidence for this cell's redo now lives, and a study whose history says a
    derivative was retired but not to where has kept the fact and lost the pointer.
    """
    where = f"{study_rel}/{derivative_rel}" if str(study_rel) != "." else derivative_rel
    what = "--remove" if dest is None else f"--path {dest}"
    return f"mechababs retire-derivative {where} {what} (campaign {label!r})"


def run_retire(path, *, dest=None, remove=False, root="."):
    """Retire one derivative. Returns a CLI exit code.

    Exactly one of ``dest`` / ``remove``; the CLI's mutually-exclusive required group
    is what enforces that, and this asserts it rather than choosing a winner.
    """
    assert bool(dest) != bool(remove), "exactly one of --path / --remove"
    selected = campaign_mod.require_selected_campaign(root)
    root, label = selected.root, selected.label

    study_rel, derivative_rel = parse_derivative_path(root, path)
    study = resolve_study(root, label, study_rel)
    if not (study / derivative_rel).is_dir():
        sys.exit(f"no such derivative: {study / derivative_rel}")
    require_claimed_cell(study, label, derivative_rel)

    # Both roots (the same directory at a lone study): the superstudy is a published
    # object too.
    target = (
        None
        if remove
        else next_attempt(
            require_outside(dest, study, root), study.name, Path(derivative_rel).name
        )
    )

    # The level's single writer, held across the whole read-modify-write.
    with utils.flocked(campaign_mod.flock_path(selected.operated_at)):
        _retire(root, study, label, study_rel, derivative_rel, target)

    if target is None:
        print(f"removed {study / derivative_rel}", file=sys.stderr)
    else:
        print(f"retired {study / derivative_rel} -> {target}", file=sys.stderr)
    print(
        "The cell is back to not started; `mechababs iterate` re-scaffolds it.",
        file=sys.stderr,
    )
    return 0


def _retire(root, study, label, study_rel, derivative_rel, target):
    """The transition itself: one commit per level, the study's nested in the super's.

    The superstudy's scope is entered FIRST so its clean-in runs while the member is
    still clean — opened the other way round it would see its own intended change
    (the member's moved gitlink) as pre-existing dirt and refuse. A study-configured
    campaign has no outer level and takes the same path with it omitted.
    """
    label_text = message(label, study_rel, derivative_rel, target)

    if str(study_rel) != ".":
        with utils.campaign_save_scope(root, study) as super_save:
            with utils.campaign_save_scope(
                study, study_scope(study, label, derivative_rel)
            ) as save:
                detach(study, derivative_rel, target)
                reset_cell(study, label, derivative_rel)
                save.message = label_text
            super_save.message = label_text
        return

    with utils.campaign_save_scope(
        study, study_scope(study, label, derivative_rel)
    ) as save:
        detach(study, derivative_rel, target)
        reset_cell(study, label, derivative_rel)
        save.message = label_text
