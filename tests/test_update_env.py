"""Unit tests for `mechababs campaign update-env`.

The verb is two uv commands and a plain save, so what is worth pinning is exactly
that: which uv commands (and their flags), what gets committed and when, and the
two refusals that keep it an outer command. uv and datalad are both stubbed — the
real uv behaviors this design leans on are pinned by the `uv_build` contract tests,
and the real commit shape by the datalad integration tests.
"""

from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from conftest import stamp_dataset_id
from mechababs import campaign as campaign_mod
from mechababs import campaign_init, update_env, utils


@pytest.fixture
def uv_calls(monkeypatch):
    """Record the uv commands instead of running them."""
    calls = []

    def fake_run_uv(*args, campaign, cluster_file, uv=None, retry=None):
        calls.append(
            {"args": list(args), "campaign": campaign, "uv": uv, "retry": retry}
        )

    monkeypatch.setattr(campaign_init, "run_uv", fake_run_uv)
    return calls


@pytest.fixture
def saves(monkeypatch):
    """Record what was committed where, without a real dataset.

    ``shallow_status`` answers "dirty" by default so the save path is the one under
    test; a test that wants the nothing-changed branch overrides it.
    """
    calls = []

    monkeypatch.setattr(utils, "shallow_status", lambda root, *paths: [" M dirty"])
    monkeypatch.setattr(
        utils,
        "save_paths",
        lambda root, paths, message: calls.append(("save", Path(root), paths, message)),
    )

    @contextmanager
    def null_scope(root, path):
        pending = utils.PendingSave()
        yield pending
        calls.append(("scope", Path(root), path, pending.message))

    monkeypatch.setattr(utils, "campaign_save_scope", null_scope)
    return calls


def make_campaign(root, label="nprep", *, superstudy=False, lock="lock-v1\n"):
    """A campaign footprint complete enough for update-env: config, pyproject, lock."""
    campaign = campaign_mod.campaign_dir(root, label)
    campaign.mkdir(parents=True)
    (root / ".datalad").mkdir(exist_ok=True)
    campaign_mod.config_path(root, label).write_text(
        yaml.safe_dump({"label": label, "cluster": "clusters/dartmouth.yaml"})
    )
    campaign_mod.pyproject_path(root, label).write_text("[project]\n")
    campaign_mod.uv_lock_path(root, label).write_text(lock)
    if superstudy:
        campaign_mod.members_path(root, label).write_text(
            campaign_mod.initial_members_header()
        )
    else:
        campaign_mod.state_path(root, label).write_text(campaign_mod.initial_header())
    return campaign


@pytest.fixture
def study(tmp_path, monkeypatch):
    root = tmp_path / "study-ds000001"
    root.mkdir()
    make_campaign(root)
    monkeypatch.setenv(campaign_mod.CAMPAIGN_ENV_VAR, "nprep")
    return root


@pytest.fixture
def superstudy(tmp_path, monkeypatch):
    """A superstudy campaign with one member carrying the footprint."""
    root = tmp_path / "my-super"
    root.mkdir()
    make_campaign(root, superstudy=True, lock="lock-v2\n")
    stamp_dataset_id(root)

    member = root / "study-ds000001"
    member.mkdir()
    (member / ".datalad").mkdir()
    campaign_mod.campaign_dir(member, "nprep").mkdir(parents=True)
    campaign_mod.config_path(member, "nprep").write_text(
        f"label: nprep\n{campaign_mod.SUPERSTUDY_KEY}: {stamp_dataset_id(root)}\n"
    )
    campaign_mod.uv_lock_path(member, "nprep").write_text("lock-v1\n")  # behind
    campaign_mod.state_path(member, "nprep").write_text(campaign_mod.initial_header())

    monkeypatch.setenv(campaign_mod.CAMPAIGN_ENV_VAR, "nprep")
    return root, member


# --- what it asks uv --------------------------------------------------------


def test_bare_update_env_locks_then_syncs_frozen(study, uv_calls, saves):
    """Two commands, in this order. `lock` re-resolves the declaration; `sync
    --frozen` installs exactly what the lock now says and never re-resolves behind
    it, so the venv can only ever be the lock's."""
    update_env.run_update_env(study)

    campaign = campaign_mod.campaign_dir(study, "nprep")
    assert [c["args"] for c in uv_calls] == [
        ["lock", "--project", str(campaign)],
        ["sync", "--project", str(campaign), "--frozen"],
    ]


def test_upgrade_appends_one_upgrade_package_per_name(study, uv_calls, saves):
    """A pure passthrough, repeatable. It reaches only `lock` — the sync installs
    whatever that produced — and it never touches the pyproject: the declaration
    states the intent ("track this branch"), and --upgrade re-resolves it."""
    before = campaign_mod.pyproject_path(study, "nprep").read_text()

    update_env.run_update_env(study, upgrade=["babs", "mechababs"])

    campaign = campaign_mod.campaign_dir(study, "nprep")
    assert uv_calls[0]["args"] == [
        "lock",
        "--project",
        str(campaign),
        "--upgrade-package",
        "babs",
        "--upgrade-package",
        "mechababs",
    ]
    assert "--upgrade-package" not in " ".join(uv_calls[1]["args"])
    assert campaign_mod.pyproject_path(study, "nprep").read_text() == before


def test_no_upgrade_means_a_plain_relock(study, uv_calls, saves):
    """Bare is not `--upgrade *`. A branch pin already locked stays at its sha, which
    is what makes bare update-env safe as rebuild-from-lock in every recovery
    story."""
    update_env.run_update_env(study)
    assert not any("--upgrade-package" in c["args"] for c in uv_calls)


def test_the_uv_that_runs_is_this_venvs_when_there_is_one(tmp_path, monkeypatch):
    """Preferred, not required — which is the whole difference from the guards.

    `uv_bin()` insists on the campaign venv's uv, because a guard runs only when
    there IS a campaign venv. update-env runs precisely when there may not be one,
    so it falls back to PATH rather than becoming unavailable when it is needed
    most.
    """
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    assert update_env.uv_for_update(prefix=venv) == campaign_init.UV

    (venv / "bin" / "uv").write_text("#!/bin/sh\n")
    assert update_env.uv_for_update(prefix=venv) == str(venv / "bin" / "uv")


def test_a_build_failure_is_pointed_at_the_campaigns_own_cluster_config(
    study, uv_calls, saves
):
    """The STAGED copy, which is the file committed with the campaign and so the one
    to edit — not whatever path the config was originally given as."""
    update_env.run_update_env(study)
    expected = campaign_mod.clusters_dir(study, "nprep") / "dartmouth.yaml"
    assert all(c["campaign"] for c in uv_calls)
    assert update_env.staged_cluster(study, "nprep") == expected


def test_a_build_failure_tells_the_user_to_re_run_update_env_not_to_delete(
    study, uv_calls, saves
):
    """init's tail says to `rm -rf` the half-built campaign, because init cannot
    re-run over an existing one. Here that would delete a campaign holding real
    derivatives and history — update-env converges an existing campaign, so the way
    back is simply to run it again."""
    update_env.run_update_env(study)

    assert all(c["retry"] == campaign_init.UPDATE_ENV_RETRY for c in uv_calls)
    assert "rm -rf" not in campaign_init.UPDATE_ENV_RETRY


# --- what it commits --------------------------------------------------------


def test_the_declaration_and_the_lock_are_committed_together(study, uv_calls, saves):
    """One commit carrying both halves: the intent the user edited and the
    resolution uv produced from it. Scoped to those two files, so nothing else in
    flight is swept in."""
    update_env.run_update_env(study)

    (kind, root, paths, message) = saves[0]
    assert kind == "save"
    assert root == study
    assert sorted(paths) == sorted(
        [
            str(Path(".mechababs/campaigns/nprep/pyproject.toml")),
            str(Path(".mechababs/campaigns/nprep/uv.lock")),
        ]
    )
    assert "update-env" in message


def test_nothing_is_committed_when_neither_file_moved(
    study, uv_calls, saves, monkeypatch
):
    """The rebuild-from-lock case — a fresh clone, a wiped site — where the venv is
    built and there is genuinely nothing new to record."""
    monkeypatch.setattr(utils, "shallow_status", lambda root, *paths: [])

    update_env.run_update_env(study)

    assert uv_calls, "the environment is still built"
    assert saves == []


def test_the_commit_message_names_the_upgrade_flags(study, uv_calls, saves):
    """The lock diff is the record of what moved; the message says what asked."""
    update_env.run_update_env(study, upgrade=["babs"])
    assert "--upgrade babs" in saves[0][3]


# --- the member's lock copy -------------------------------------------------


def test_study_copies_the_lock_down_and_commits_at_both_levels(
    superstudy, uv_calls, saves
):
    """Every level stays clean: the member commits its lock, then the super commits
    the gitlink pointing at it. The super's scope opens FIRST so its clean-in runs
    while the member is still clean."""
    root, member = superstudy

    update_env.run_update_env(root, member="study-ds000001")

    assert campaign_mod.uv_lock_path(member, "nprep").read_text() == "lock-v2\n"
    scopes = [c for c in saves if c[0] == "scope"]
    # entered super-then-member, so they CLOSE member-then-super
    assert [c[1] for c in scopes] == [member, root]
    assert all("update-env --study study-ds000001" in c[3] for c in scopes)


def test_study_copies_the_lock_and_nothing_else(superstudy, uv_calls, saves):
    """The lock ONLY. A member's footprint is its own after creation and may carry
    deliberate per-study config overrides, so a blind config copy would clobber
    them — how a canonical config edit propagates is a separate, open question."""
    root, member = superstudy
    campaign_mod.config_path(member, "nprep").write_text("label: nprep\nmine: true\n")

    update_env.run_update_env(root, member="study-ds000001")

    assert "mine: true" in campaign_mod.config_path(member, "nprep").read_text()


def test_no_uv_runs_at_the_member(superstudy, uv_calls, saves):
    """Resolution happens only where the pyproject lives. The member is handed a
    result, never asked to compute one — which is why it needs no pyproject of its
    own to be refreshed and no venv to be built."""
    root, member = superstudy

    update_env.run_update_env(root, member="study-ds000001")

    assert all(
        c["campaign"] == campaign_mod.campaign_dir(root, "nprep") for c in uv_calls
    )


def test_a_member_that_was_never_selected_is_refused(superstudy, uv_calls, saves):
    """No footprint means no lock copy to refresh, and writing one would be selecting
    the member into the campaign — add-dataset's decision, not an env update's side
    effect."""
    root, _ = superstudy
    stranger = root / "study-ds000002"
    (stranger / ".datalad").mkdir(parents=True)

    with pytest.raises(SystemExit, match="carries no campaign"):
        update_env.run_update_env(root, member="study-ds000002")


def test_a_study_outside_the_superstudy_is_refused(superstudy, uv_calls, saves):
    root, _ = superstudy
    with pytest.raises(SystemExit, match="not inside this superstudy"):
        update_env.run_update_env(root, member="../elsewhere")


# --- it is still an outer command -------------------------------------------


def test_study_is_refused_for_a_study_configured_campaign(study, uv_calls, saves):
    """Both directions of the configured-level rule, as everywhere else: a
    study-configured campaign has no member to name."""
    with pytest.raises(SystemExit, match="no member to name"):
        update_env.run_update_env(study, member="study-ds000001")


def test_update_env_refuses_at_a_member(superstudy, uv_calls, saves):
    """The guard exemption is the ENV check only, not the level rule. The
    environment lives at the superstudy, so that is where it is updated — reaching a
    member is what --study is for."""
    _, member = superstudy

    with pytest.raises(SystemExit, match="operated from its superstudy"):
        update_env.run_update_env(member)


def test_update_env_needs_a_campaign_to_be_selected(study, monkeypatch):
    monkeypatch.delenv(campaign_mod.CAMPAIGN_ENV_VAR, raising=False)
    with pytest.raises(SystemExit):
        update_env.run_update_env(study)


def test_update_env_names_the_campaign_it_cannot_find(tmp_path, monkeypatch):
    root = tmp_path / "study-ds000001"
    (root / ".datalad").mkdir(parents=True)
    monkeypatch.setenv(campaign_mod.CAMPAIGN_ENV_VAR, "nope")
    with pytest.raises(SystemExit, match="no campaign 'nope'"):
        update_env.run_update_env(root)


def test_update_env_runs_without_the_env_match_guard(study, uv_calls, saves):
    """The exemption itself: `sys.prefix` is some unrelated interpreter and there is
    no venv at all, which is exactly the state this command exists to fix. Anything
    calling `require_env_match` here would refuse and leave the user stuck."""
    assert not campaign_mod.venv_path(study, "nprep").exists()

    update_env.run_update_env(study)

    assert uv_calls


# --- the single writer ------------------------------------------------------


def test_the_campaign_flock_is_held_across_the_whole_update(study, saves, monkeypatch):
    """The level's single-writer lock, held because this rewrites the campaign's
    uv.lock — the very file `iterate` dispatches work against. So an iterate must not read it mid-rewrite,
    and two update-envs must not resolve into it at once.

    Held across everything, not just the save: the window that matters opens at
    `uv lock` (which rewrites uv.lock in place) and closes after the commit.
    """
    held = []
    real_flocked = update_env.utils.flocked

    @contextmanager
    def watching_flock(lock):
        with real_flocked(lock):
            held.append(Path(lock))
            yield
            held.append("released")

    monkeypatch.setattr(update_env.utils, "flocked", watching_flock)

    def fake_run_uv(*args, campaign, cluster_file, uv=None, retry=None):
        assert held and held[-1] != "released", f"uv ran unlocked: {args[0]}"

    monkeypatch.setattr(campaign_init, "run_uv", fake_run_uv)

    update_env.run_update_env(study)

    assert held[0] == campaign_mod.flock_path(study)
    assert held[-1] == "released"
    assert len([h for h in held if h != "released"]) == 1, "taken exactly once"


def test_the_flock_is_taken_at_the_operated_level(
    superstudy, uv_calls, saves, monkeypatch
):
    """At the superstudy, not the member — the same level `iterate`'s fan-out locks,
    which is what makes the two mutually exclusive. A member-keyed lock would let a
    iterate advance the member while its lock copy is being rewritten."""
    taken = []
    real_flocked = update_env.utils.flocked

    @contextmanager
    def recording_flock(lock):
        taken.append(Path(lock))
        with real_flocked(lock):
            yield

    monkeypatch.setattr(update_env.utils, "flocked", recording_flock)
    root, _ = superstudy

    update_env.run_update_env(root, member="study-ds000001")

    assert taken == [campaign_mod.flock_path(root)]
