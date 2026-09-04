"""Unit tests for the study-root check.

Every command operates inside a study that already exists, so the
wrong-directory mistake is caught once, here, rather than as a confusing failure
deeper in.
"""

import pytest

from mechababs import study as study_mod


def test_a_datalad_dataset_is_a_study_root(tmp_path):
    (tmp_path / ".datalad").mkdir()
    assert study_mod.is_study_root(tmp_path)
    assert study_mod.require_study_root(tmp_path) == tmp_path.resolve()


def test_a_plain_git_repo_counts(tmp_path):
    # fixture studies (and the e2e's fabricated one) are plain git repos
    (tmp_path / ".git").mkdir()
    assert study_mod.is_study_root(tmp_path)


def test_a_worktree_git_file_counts(tmp_path):
    # a git worktree's .git is a FILE, not a dir
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    assert study_mod.is_study_root(tmp_path)


def test_a_bare_directory_is_not_a_study(tmp_path):
    assert not study_mod.is_study_root(tmp_path)
    with pytest.raises(SystemExit):
        study_mod.require_study_root(tmp_path)
