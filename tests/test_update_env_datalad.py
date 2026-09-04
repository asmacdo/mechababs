"""`campaign update-env` against real datalad datasets: what actually gets committed.

The unit tests stub the saves to pin *which* paths and messages are asked for. These
build real datasets, because the two properties worth proving cannot be stubbed: that
a commit lands at each level, and that **every level is left clean**. A save that
half-lands — the member advanced, the superstudy's gitlink still pointing at the old
commit — reads as clean at the member and dirty at the super, and the next iterate then
refuses. That is a shape only a real dataset shows.

uv is stubbed here (the real uv is pinned by the `uv_build` contract tests); datalad
is not.
"""

import subprocess

import pytest
import yaml
from datalad.api import Dataset

from mechababs import campaign as campaign_mod
from mechababs import campaign_init, update_env


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commits(root, *paths):
    """Commit subjects touching ``paths``, newest first."""
    out = git(root, "log", "--format=%s", "--", *[str(p) for p in paths])
    return [line for line in out.splitlines() if line]


def is_clean(root):
    return git(root, "status", "--porcelain") == ""


def write_campaign(root, label="nprep", *, superstudy=False, lock="lock-v1\n"):
    """A campaign footprint, written the way `campaign init` leaves one."""
    campaign = campaign_mod.campaign_dir(root, label)
    campaign.mkdir(parents=True)
    campaign_mod.level_gitignore_path(root).write_text(
        f"{campaign_mod.FLOCK_FILENAME}\n"
    )
    (campaign / ".gitattributes").write_text(campaign_init.GITATTRIBUTES)
    (campaign / ".gitignore").write_text(f"{campaign_mod.VENV_DIRNAME}/\n")
    campaign_mod.config_path(root, label).write_text(
        yaml.safe_dump({"label": label, "cluster": "clusters/dartmouth.yaml"})
    )
    campaign_mod.pyproject_path(root, label).write_text("[project]\n# v1\n")
    campaign_mod.uv_lock_path(root, label).write_text(lock)
    if superstudy:
        campaign_mod.members_path(root, label).write_text(
            campaign_mod.initial_members_header()
        )
    else:
        campaign_mod.state_path(root, label).write_text(campaign_mod.initial_header())
    return campaign


@pytest.fixture
def fake_uv(monkeypatch):
    """Stand in for uv: `lock` rewrites the lock, `sync` does nothing visible.

    Controllable, because the point of these tests is what mechababs does with the
    two outcomes — a lock that moved and a lock that did not.
    """
    state = {"new_lock": "lock-v2\n"}

    def fake_run_uv(*args, campaign, cluster_file, uv=None, retry=None):
        if args and args[0] == "lock" and state["new_lock"] is not None:
            (campaign / campaign_mod.UV_LOCK_FILENAME).write_text(state["new_lock"])

    monkeypatch.setattr(campaign_init, "run_uv", fake_run_uv)
    return state


@pytest.fixture
def study(tmp_path, monkeypatch):
    root = tmp_path / "study-ds000001"
    Dataset(str(root)).create(cfg_proc="text2git", result_renderer="disabled")
    write_campaign(root)
    Dataset(str(root)).save(message="campaign init", result_renderer="disabled")
    monkeypatch.setenv(campaign_mod.CAMPAIGN_ENV_VAR, "nprep")
    assert is_clean(root)
    return root


@pytest.fixture
def superstudy(tmp_path, monkeypatch):
    """A real superstudy with a real member subdataset, both clean."""
    root = tmp_path / "my-super"
    ds = Dataset(str(root))
    ds.create(cfg_proc="text2git", result_renderer="disabled")
    write_campaign(root, superstudy=True)

    member = root / "study-ds000001"
    ds.create(path=str(member), cfg_proc="text2git", result_renderer="disabled")
    campaign = campaign_mod.campaign_dir(member, "nprep")
    campaign.mkdir(parents=True)
    (campaign / ".gitattributes").write_text(campaign_init.GITATTRIBUTES)
    (campaign / ".gitignore").write_text(f"{campaign_mod.VENV_DIRNAME}/\n")
    campaign_mod.config_path(member, "nprep").write_text(
        f"label: nprep\n{campaign_mod.SUPERSTUDY_KEY}: {Dataset(str(root)).id}\n"
    )
    campaign_mod.uv_lock_path(member, "nprep").write_text("lock-v1\n")
    campaign_mod.state_path(member, "nprep").write_text(campaign_mod.initial_header())

    ds.save(recursive=True, message="campaign init", result_renderer="disabled")
    monkeypatch.setenv(campaign_mod.CAMPAIGN_ENV_VAR, "nprep")
    assert is_clean(member) and is_clean(root)
    return root, member


# --- bare update-env --------------------------------------------------------


def test_the_declaration_and_the_lock_land_in_one_commit(study, fake_uv):
    """Both halves together: the intent the user edited and the resolution it
    produced. Splitting them would leave a commit claiming a resolution for a
    declaration that is not in the history yet."""
    campaign_mod.pyproject_path(study, "nprep").write_text("[project]\n# v2\n")

    update_env.run_update_env(study)

    assert is_clean(study)
    pyproject = campaign_mod.pyproject_path(study, "nprep")
    lock = campaign_mod.uv_lock_path(study, "nprep")
    assert commits(study, pyproject)[0] == commits(study, lock)[0]
    assert "update-env" in commits(study, lock)[0]
    assert lock.read_text() == "lock-v2\n"


def test_a_hand_edited_pyproject_is_committed_rather_than_refused(study, fake_uv):
    """The documented way to bump a campaign is to edit the pyproject by hand and
    run this — so the declaration is DIRTY by design when the command arrives. A
    clean-in guard here would refuse the command's primary use."""
    campaign_mod.pyproject_path(study, "nprep").write_text("[project]\n# bumped\n")
    assert not is_clean(study)

    update_env.run_update_env(study)

    assert is_clean(study)
    assert "# bumped" in git(
        study, "show", "HEAD:.mechababs/campaigns/nprep/pyproject.toml"
    )


def test_nothing_is_committed_when_the_lock_does_not_move(study, fake_uv):
    """Rebuild-from-lock: a fresh clone, a wiped site, a historical checkout. The
    venv is rebuilt and there is genuinely nothing new to record, so the history
    does not gain a commit that says nothing happened."""
    fake_uv["new_lock"] = None  # uv lock is a no-op: the declaration is unchanged
    before = git(study, "rev-parse", "HEAD")

    update_env.run_update_env(study)

    assert git(study, "rev-parse", "HEAD") == before
    assert is_clean(study)


def test_unrelated_work_in_flight_is_not_swept_into_the_commit(study, fake_uv):
    """The save is scoped to the two declaration files, and this is the reason it
    has to be: update-env deliberately commits a dirty tree (the hand-edited
    pyproject above), so an unscoped save would take everything else with it."""
    (study / "unrelated.txt").write_text("someone else's edit\n")

    update_env.run_update_env(study)

    changed = git(study, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert sorted(changed) == [
        ".mechababs/campaigns/nprep/uv.lock",
    ]
    assert not is_clean(study), "the unrelated edit is left alone, not committed"


# --- update-env --study: the member's lock copy ------------------------------


def test_study_commits_at_the_member_then_the_gitlink_at_the_super(superstudy, fake_uv):
    """Every level stays clean — the reason the copy is not left for publish time. The member commits its lock; the superstudy commits the
    gitlink that points at that commit. Neither is left dirty for a later iterate to
    trip over."""
    root, member = superstudy

    update_env.run_update_env(root, member="study-ds000001")

    assert is_clean(member), "the member is left clean"
    assert is_clean(root), "the superstudy is left clean, gitlink and all"

    assert campaign_mod.uv_lock_path(member, "nprep").read_text() == "lock-v2\n"
    assert (
        "update-env --study study-ds000001"
        in commits(member, campaign_mod.uv_lock_path(member, "nprep"))[0]
    )
    assert "update-env --study study-ds000001" in commits(root, member)[0]


def test_the_super_commits_its_own_lock_and_the_members_separately(superstudy, fake_uv):
    """The canonical update happens first and stands on its own: `--study` is an
    additional, separately-recorded act, not a variant of the same commit. So the
    history reads as "the campaign moved" then "this member was moved onto it"."""
    root, member = superstudy

    update_env.run_update_env(root, member="study-ds000001")

    assert campaign_mod.uv_lock_path(root, "nprep").read_text() == "lock-v2\n"
    canonical = commits(root, campaign_mod.uv_lock_path(root, "nprep"))
    assert "--study" not in canonical[0]


def test_the_members_configs_are_untouched_by_a_lock_refresh(superstudy, fake_uv):
    """The lock ONLY. A member may carry deliberate per-study config overrides, and
    a blind copy would clobber them — how a canonical config edit propagates is a
    separate, open question, not something an environment update decides."""
    root, member = superstudy
    config = campaign_mod.config_path(member, "nprep")
    config.write_text(config.read_text() + "local_override: true\n")
    Dataset(str(member)).save(message="member override", result_renderer="disabled")
    Dataset(str(root)).save(message="record it", result_renderer="disabled")

    update_env.run_update_env(root, member="study-ds000001")

    assert "local_override: true" in config.read_text()
    assert is_clean(member) and is_clean(root)


def test_a_member_already_at_the_lock_is_a_no_op(superstudy, fake_uv):
    """Idempotent: running it twice records the acknowledgment once."""
    root, member = superstudy
    update_env.run_update_env(root, member="study-ds000001")
    fake_uv["new_lock"] = None  # the canonical lock has stopped moving
    member_head = git(member, "rev-parse", "HEAD")
    super_head = git(root, "rev-parse", "HEAD")

    update_env.run_update_env(root, member="study-ds000001")

    assert git(member, "rev-parse", "HEAD") == member_head
    assert git(root, "rev-parse", "HEAD") == super_head
    assert is_clean(member) and is_clean(root)


# --- campaign init leaves no environment metadata ---------------------------


def test_campaign_init_commits_no_environment_metadata(tmp_path, monkeypatch):
    """The deletion, visible in the committed tree: a campaign records its
    environment as the declaration and the lock, and nothing else. The venv itself
    is gitignored, so what init commits is exactly two environment files.

    (That the guard then WORKS against a really-built venv, with nothing else
    written, is the `uv_build` test in test_campaign_init.py — it needs a real uv,
    not a real datalad.)
    """
    study = tmp_path / "study-ds000001"
    Dataset(str(study)).create(cfg_proc="text2git", result_renderer="disabled")
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "MRIQC-24.0.2.yaml").write_text("bids_app_args: {}\n")
    (configs / "dartmouth.yaml").write_text("cluster_resources: {}\n")

    def fake_build_env(campaign, cluster_file):
        venv = campaign / campaign_mod.VENV_DIRNAME
        venv.mkdir()
        (campaign / campaign_mod.UV_LOCK_FILENAME).write_text("# resolved\n")
        return venv

    monkeypatch.setattr(campaign_init, "build_env", fake_build_env)

    campaign_init.init(
        study,
        "nprep",
        [str(configs / "MRIQC-24.0.2.yaml")],
        str(configs / "dartmouth.yaml"),
    )

    assert is_clean(study), "the venv is gitignored, so init leaves a clean study"
    tracked = git(study, "ls-files", ".mechababs/campaigns/nprep").splitlines()
    env_files = [f for f in tracked if "pyproject" in f or "lock" in f or "venv" in f]
    assert sorted(env_files) == [
        ".mechababs/campaigns/nprep/pyproject.toml",
        ".mechababs/campaigns/nprep/uv.lock",
    ]
    assert not any(".mechababs-env" in f for f in tracked)
