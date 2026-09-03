"""The once-per-iterate shallow clean check — the `--explicit` backstop.

Plain git repos, not datalad datasets: the check is a `git status`, and building
real datasets here would make a unit test pay for annex init to prove something
about git's `--ignore-submodules` flag.
"""

import subprocess

import pytest

from mechababs import utils


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.org")
    _git(path, "config", "user.name", "t")
    (path / "seed").write_text("seed\n")
    _git(path, "add", "seed")
    _git(path, "commit", "-qm", "seed")
    return path


@pytest.fixture
def super_and_sub(tmp_path):
    """A repo with a submodule, both committed and clean."""
    sub = _repo(tmp_path / "sub")
    root = _repo(tmp_path / "root")
    _git(
        root,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(sub),
        "sourcedata/raw",
    )
    _git(root, "commit", "-qm", "register sub")
    return root, root / "sourcedata" / "raw"


def test_a_clean_root_passes(super_and_sub):
    root, _ = super_and_sub
    assert utils.shallow_status(root) == []
    utils.require_clean_shallow(root)


def test_a_modification_at_the_root_is_refused(super_and_sub):
    root, _ = super_and_sub
    (root / "seed").write_text("edited\n")
    with pytest.raises(RuntimeError, match="not clean"):
        utils.require_clean_shallow(root)


def test_an_untracked_file_at_the_root_is_refused(super_and_sub):
    """Untracked counts: `--explicit` would leave it behind rather than record it."""
    root, _ = super_and_sub
    (root / "stray.txt").write_text("who wrote this\n")
    with pytest.raises(RuntimeError, match="stray.txt"):
        utils.require_clean_shallow(root)


def test_a_dirty_submodule_WORKTREE_is_deliberately_not_flagged(super_and_sub):
    """The shallowness, asserted so it cannot be 'fixed' by accident.

    Walking a source dataset's working tree is exactly the cost this check exists
    to avoid, and a dirty raw input is not something a scaffold commit could
    absorb anyway — the run declares its outputs, and this is not one of them.
    """
    root, sub = super_and_sub
    (sub / "seed").write_text("edited inside the submodule\n")
    (sub / "untracked-in-sub").write_text("x\n")
    assert utils.shallow_status(root) == []
    utils.require_clean_shallow(root)


def test_a_moved_submodule_POINTER_is_flagged(super_and_sub):
    """The gitlink compare the spec asks for: the submodule's recorded commit
    moved, which IS a change to this dataset and would land in the next commit."""
    root, sub = super_and_sub
    (sub / "seed").write_text("a real commit in the submodule\n")
    _git(sub, "commit", "-qam", "advance the submodule")
    dirty = utils.shallow_status(root)
    assert dirty and "sourcedata/raw" in dirty[0], dirty
    with pytest.raises(RuntimeError, match="sourcedata/raw"):
        utils.require_clean_shallow(root)


def test_the_refusal_names_what_it_refused(super_and_sub):
    root, _ = super_and_sub
    (root / "stray.txt").write_text("x\n")
    with pytest.raises(RuntimeError, match="dispatching scaffold"):
        utils.require_clean_shallow(root, what="dispatching scaffold")


def test_ignore_excludes_a_moved_pointer_that_someone_else_checks(super_and_sub):
    """At a superstudy each member's pointer is checked separately, right before that
    member is advanced — so the level's own check ignores them, and one member's
    drift stops that member instead of the whole fan-out."""
    root, sub = super_and_sub
    (sub / "seed").write_text("a real commit in the submodule\n")
    _git(sub, "commit", "-qam", "advance the submodule")

    assert utils.shallow_status(root)  # unignored, it is dirt
    utils.require_clean_shallow(root, ignore=["sourcedata/raw"])  # ignored, it passes


def test_ignore_does_not_blind_the_check_to_the_level_s_own_tree(super_and_sub):
    """Ignoring the members must not amount to ignoring everything: what is left is
    exactly the dirt only this level can see."""
    root, sub = super_and_sub
    (sub / "seed").write_text("a real commit in the submodule\n")
    _git(sub, "commit", "-qam", "advance the submodule")
    (root / "stray.txt").write_text("x\n")

    with pytest.raises(RuntimeError, match="stray.txt"):
        utils.require_clean_shallow(root, ignore=["sourcedata/raw"])
