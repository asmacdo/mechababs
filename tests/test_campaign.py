"""Unit tests for campaign selection and the env-match guard.

The guard is what keeps a run from being recorded against tools that did not
produce it, so both drift directions (wrong venv entirely; right venv, moved lock)
are tested explicitly.
"""

import shutil
import sys

import pytest

from conftest import stamp_dataset_id
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
    selected = campaign_mod.require_selected_campaign(tmp_path)
    assert selected == (
        tmp_path.resolve(),
        "nprep",
        campaign_mod.campaign_dir(tmp_path, "nprep"),
        tmp_path.resolve(),
    )
    # A study-configured campaign is operated where it stands: the two levels are
    # the same directory, which is what makes the distinction invisible until a
    # superstudy separates them.
    assert selected.operated_at == selected.root


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


# --- the operated level, the distinction the whole layer turns on -----------


def test_operated_level_is_the_super_for_a_member_and_itself_for_a_study(tmp_path):
    """The two levels coincide for a study and diverge for a member.

    Every environment-shaped question — the venv, env.sh, the lock that built it,
    the single writer — is asked of this and not of the study, because a member is
    given none of them.
    """
    from mechababs import campaign as c

    member = tmp_path / "study-ds000001"
    c.campaign_dir(member, "nprep").mkdir(parents=True)
    c.config_path(member, "nprep").write_text(
        f"label: nprep\nsuperstudy: {stamp_dataset_id(tmp_path)}\n"
    )
    assert c.operated_level(member, "nprep") == tmp_path.resolve()

    lone = tmp_path / "study-ds000002"
    c.campaign_dir(lone, "nprep").mkdir(parents=True)
    c.config_path(lone, "nprep").write_text("label: nprep\n")
    assert c.operated_level(lone, "nprep") == lone


def test_a_member_cloned_standalone_reads_as_detached(tmp_path):
    """A member cloned away from its superstudy operates on its own contents.

    This is what `write_member_footprint` copies the lock down FOR — "the member
    reproduces its own derivatives from its own contents, without the superstudy" —
    and it is reached through `require_env_match`, which `mechababs-inner` calls. So
    resolving the level wrongly here does not merely inconvenience: it breaks
    `datalad rerun` of the study's own recorded commands, which is the whole
    re-executability claim.

    A relative marker could not express this. `..` resolves against wherever the
    clone now sits, so the member silently adopted an unrelated parent directory as
    the place its environment lives.
    """
    from mechababs import campaign as c

    super_root = tmp_path / "my-super"
    member = super_root / "study-ds000001"
    c.campaign_dir(member, "nprep").mkdir(parents=True)
    c.config_path(member, "nprep").write_text(
        f"label: nprep\nsuperstudy: {stamp_dataset_id(super_root)}\n"
    )
    assert c.operated_level(member, "nprep") == super_root.resolve()

    # The same member, cloned somewhere with an unrelated directory above it.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.copytree(member, elsewhere / "study-ds000001")
    standalone = elsewhere / "study-ds000001"
    assert c.operated_level(standalone, "nprep") == standalone
    assert c.superstudy_of(standalone, "nprep") is None


def test_a_member_cloned_into_a_different_superstudy_is_not_adopted(tmp_path):
    """Presence above is not ownership. The other super is a real superstudy with a
    real campaign dir — only the id distinguishes it from ours, which is why a path
    marker (always `..`) could never answer this."""
    from mechababs import campaign as c

    ours = tmp_path / "ours"
    member = ours / "study-ds000001"
    c.campaign_dir(member, "nprep").mkdir(parents=True)
    c.config_path(member, "nprep").write_text(
        f"label: nprep\nsuperstudy: {stamp_dataset_id(ours)}\n"
    )

    theirs = tmp_path / "theirs"
    theirs.mkdir()
    stamp_dataset_id(theirs, "99999999-8888-7777-6666-555555555555")
    shutil.copytree(member, theirs / "study-ds000001")

    assert c.superstudy_of(theirs / "study-ds000001", "nprep") is None


# --- the configured-level rule, in the shared precondition ------------------


def test_a_member_of_a_super_campaign_refuses_before_the_env_guard(
    tmp_path, monkeypatch
):
    """The level check comes first on purpose: a member carries no venv of its own,
    so the env guard reached first would name an env.sh that will never exist."""
    import pytest

    from mechababs import campaign as c

    member = tmp_path / "study-ds000001"
    c.campaign_dir(member, "nprep").mkdir(parents=True)
    c.config_path(member, "nprep").write_text(
        f"label: nprep\nsuperstudy: {stamp_dataset_id(tmp_path)}\n"
    )
    (member / ".datalad").mkdir()
    monkeypatch.setenv(c.CAMPAIGN_ENV_VAR, "nprep")

    with pytest.raises(SystemExit) as excinfo:
        c.require_selected_campaign(str(member))
    message = str(excinfo.value)
    assert "operated from its superstudy" in message
    assert "env.sh" not in message


def test_allow_member_is_the_escape_for_a_verb_that_offers_one(tmp_path, monkeypatch):
    """iterate --force advances a detached member; the user owns reconciling it."""
    import pytest

    from mechababs import campaign as c

    member = tmp_path / "study-ds000001"
    c.campaign_dir(member, "nprep").mkdir(parents=True)
    c.config_path(member, "nprep").write_text(
        f"label: nprep\nsuperstudy: {stamp_dataset_id(tmp_path)}\n"
    )
    (member / ".datalad").mkdir()
    monkeypatch.setenv(c.CAMPAIGN_ENV_VAR, "nprep")

    # gets past the level check, and on to the env guard — a different refusal
    with pytest.raises(SystemExit) as excinfo:
        c.require_selected_campaign(str(member), allow_member=True)
    assert "operated from its superstudy" not in str(excinfo.value)


def test_a_study_campaign_has_no_superstudy(tmp_path):
    from mechababs import campaign as c

    study = tmp_path / "study-ds000001"
    c.campaign_dir(study, "nprep").mkdir(parents=True)
    c.config_path(study, "nprep").write_text("label: nprep\n")

    assert c.superstudy_of(study, "nprep") is None


def make_member(superstudy, label="nprep", lock_text="lock-v1\n"):
    """A member of a super-campaign, shaped as ``write_member_footprint`` leaves it.

    The whole point: config (carrying the superstudy marker) and a copy of the lock,
    but deliberately **no venv and no env.sh** — a member of a super-campaign is not
    operated from, so its environment is the superstudy's.
    """
    member = superstudy / "study-ds000001"
    cdir = campaign_mod.campaign_dir(member, label)
    cdir.mkdir(parents=True)
    campaign_mod.config_path(member, label).write_text(
        f"label: {label}\n{campaign_mod.SUPERSTUDY_KEY}: {stamp_dataset_id(superstudy)}\n"
    )
    campaign_mod.uv_lock_path(member, label).write_text(lock_text)
    return member


def test_env_match_at_a_member_resolves_the_venv_at_its_superstudy(
    tmp_path, monkeypatch
):
    """The fan-out dispatches inner verbs with the MEMBER as cwd, while the running
    interpreter is the superstudy's venv — the member has none by construction.
    Resolving the environment at the member demanded a venv that cannot exist, and
    no superstudy transition could scaffold."""
    make_campaign(tmp_path)
    member = make_member(tmp_path)
    pretend_running_in(monkeypatch, campaign_mod.venv_path(tmp_path, "nprep"))

    assert campaign_mod.require_env_match(member, "nprep") == campaign_mod.campaign_dir(
        member, "nprep"
    )


def test_env_match_at_a_member_names_the_superstudys_env_sh_when_it_refuses(
    tmp_path, monkeypatch
):
    """A member has no env.sh, so pointing at one there would send the user to a
    file that will never exist."""
    make_campaign(tmp_path)
    member = make_member(tmp_path)
    pretend_running_in(monkeypatch, tmp_path / "elsewhere")

    with pytest.raises(SystemExit) as e:
        campaign_mod.require_env_match(member, "nprep")
    assert str(campaign_mod.env_path(tmp_path, "nprep")) in str(e.value)


def test_env_match_at_a_member_still_checks_the_lock_that_built_the_venv(
    tmp_path, monkeypatch
):
    """Resolving at the operated level must not weaken the drift check: a bumped
    superstudy lock is still caught from inside a member."""
    make_campaign(tmp_path, lock_text="lock-v1\n")
    member = make_member(tmp_path, lock_text="lock-v1\n")
    campaign_mod.uv_lock_path(tmp_path, "nprep").write_text("lock-v2\n")
    pretend_running_in(monkeypatch, campaign_mod.venv_path(tmp_path, "nprep"))

    with pytest.raises(SystemExit) as e:
        campaign_mod.require_env_match(member, "nprep")
    assert "campaign update-env" in str(e.value)
