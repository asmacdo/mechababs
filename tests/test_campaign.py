"""Unit tests for campaign selection and the env-match guard.

The guard is what keeps a run from being recorded against tools that did not
produce it, so both drift directions (wrong venv entirely; right venv, moved lock)
are tested explicitly.
"""

import sys

import pytest

from mechababs import campaign as campaign_mod


def make_campaign(tmp_path, label="nprep", lock_text="lock-v1\n", *, stamp=True):
    """A campaign dir complete enough for the guard: config, lock, venv (+stamp)."""
    cdir = campaign_mod.campaign_dir(tmp_path, label)
    cdir.mkdir(parents=True)
    campaign_mod.config_path(tmp_path, label).write_text("label: nprep\n")
    campaign_mod.uv_lock_path(tmp_path, label).write_text(lock_text)
    venv = campaign_mod.venv_path(tmp_path, label)
    venv.mkdir()
    if stamp:
        campaign_mod.write_env_stamp(venv, label, lock_text)
    return cdir


def pretend_running_in(monkeypatch, venv):
    monkeypatch.setattr(sys, "prefix", str(venv))


def test_statefile_header_is_the_tall_cell_schema():
    assert campaign_mod.initial_header() == (
        "source_dataset\tapp_config\tprocessing_level\tn_subjects\tn_sessions\t"
        "depends_on\tbabs\tmerged\n"
    )


def test_selected_label_reads_the_env_var(monkeypatch):
    monkeypatch.setenv(campaign_mod.CAMPAIGN_ENV_VAR, "nprep")
    assert campaign_mod.selected_label() == "nprep"


def test_selected_label_exits_when_unset(monkeypatch):
    # no default-if-only-one: selection is always explicit
    monkeypatch.delenv(campaign_mod.CAMPAIGN_ENV_VAR, raising=False)
    with pytest.raises(SystemExit):
        campaign_mod.selected_label()


def test_env_match_passes_for_a_venv_built_from_the_committed_lock(
    tmp_path, monkeypatch
):
    make_campaign(tmp_path)
    pretend_running_in(monkeypatch, campaign_mod.venv_path(tmp_path, "nprep"))
    assert campaign_mod.require_env_match(
        tmp_path, "nprep"
    ) == campaign_mod.campaign_dir(tmp_path, "nprep")


def test_env_match_refuses_an_unknown_campaign(tmp_path):
    with pytest.raises(SystemExit):
        campaign_mod.require_env_match(tmp_path, "nope")


def test_env_match_refuses_another_python(tmp_path, monkeypatch):
    make_campaign(tmp_path)
    # an ambient install, or another campaign's venv
    pretend_running_in(monkeypatch, tmp_path / "elsewhere")
    with pytest.raises(SystemExit) as e:
        campaign_mod.require_env_match(tmp_path, "nprep")
    assert "env.sh" in str(e.value)


def test_env_match_refuses_a_venv_that_predates_a_bumped_lock(tmp_path, monkeypatch):
    make_campaign(tmp_path, lock_text="lock-v1\n")
    # the lock was bumped (mid-sweep version bump) and the venv not rebuilt
    campaign_mod.uv_lock_path(tmp_path, "nprep").write_text("lock-v2\n")
    pretend_running_in(monkeypatch, campaign_mod.venv_path(tmp_path, "nprep"))
    with pytest.raises(SystemExit) as e:
        campaign_mod.require_env_match(tmp_path, "nprep")
    assert "campaign update-env" in str(e.value)


def test_env_match_refuses_a_venv_mechababs_did_not_build(tmp_path, monkeypatch):
    make_campaign(tmp_path, stamp=False)
    pretend_running_in(monkeypatch, campaign_mod.venv_path(tmp_path, "nprep"))
    with pytest.raises(SystemExit):
        campaign_mod.require_env_match(tmp_path, "nprep")


def test_require_selected_campaign_bundles_the_three_preconditions(
    tmp_path, monkeypatch
):
    (tmp_path / ".datalad").mkdir()
    make_campaign(tmp_path)
    monkeypatch.setenv(campaign_mod.CAMPAIGN_ENV_VAR, "nprep")
    pretend_running_in(monkeypatch, campaign_mod.venv_path(tmp_path, "nprep"))
    root, label, campaign = campaign_mod.require_selected_campaign(tmp_path)
    assert (root, label, campaign) == (
        tmp_path.resolve(),
        "nprep",
        campaign_mod.campaign_dir(tmp_path, "nprep"),
    )


def test_require_selected_campaign_refuses_outside_a_study(tmp_path, monkeypatch):
    make_campaign(tmp_path)  # a campaign dir, but no dataset root
    monkeypatch.setenv(campaign_mod.CAMPAIGN_ENV_VAR, "nprep")
    with pytest.raises(SystemExit):
        campaign_mod.require_selected_campaign(tmp_path)


def test_require_statefile_returns_the_shard_when_there_is_one(tmp_path):
    make_campaign(tmp_path)
    campaign_mod.state_path(tmp_path, "nprep").write_text(campaign_mod.initial_header())
    assert campaign_mod.require_statefile(tmp_path, "nprep") == campaign_mod.state_path(
        tmp_path, "nprep"
    )


def test_require_statefile_names_the_study_superstudy_asymmetry(tmp_path):
    """A campaign dir with config but no shard is a SUPERSTUDY's — it carries
    membership instead. A verb that needs cells is at the wrong level, and that is
    a different mistake from pointing at a campaign that does not exist.
    """
    make_campaign(tmp_path)
    with pytest.raises(SystemExit, match="member study"):
        campaign_mod.require_statefile(tmp_path, "nprep")


def test_require_statefile_says_no_campaign_when_there_is_none(tmp_path):
    with pytest.raises(SystemExit, match="no campaign"):
        campaign_mod.require_statefile(tmp_path, "nprep")
