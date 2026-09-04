"""Unit tests for campaign selection and the two environment checks.

The outer guard (`require_env_match`) is what keeps a run from being recorded
against tools that did not produce it, so both of its refusals — wrong venv
entirely, and a venv uv says disagrees with the lock — are tested explicitly. The
inner verbs' check (`require_study_lock_match`) is the study-local half, and the
member hint it grows is tested against the superstudy marker that gates it.

Freshness is a `uv sync --check` subprocess, so these stub it two ways: at
`venv_matches_lock` when the test is about what the guard *does* with the answer,
and at `subprocess.run` when it is about the invocation itself. Nothing here needs a
real uv — the `uv_build`-marked contract tests are what pin uv's actual behavior.
"""

import shutil
import sys

import pytest

from conftest import pretend_uv_check, stamp_dataset_id
from mechababs import campaign as campaign_mod


def make_campaign(tmp_path, label="nprep", lock_text="lock-v1\n"):
    """A campaign dir complete enough for the guard: config, lock, venv.

    No stamp, because there is none: the only environment artifacts are the lock and
    the venv, and whether they agree is uv's answer, stubbed per test.
    """
    cdir = campaign_mod.campaign_dir(tmp_path, label)
    cdir.mkdir(parents=True)
    campaign_mod.config_path(tmp_path, label).write_text("label: nprep\n")
    campaign_mod.uv_lock_path(tmp_path, label).write_text(lock_text)
    venv = campaign_mod.venv_path(tmp_path, label)
    venv.mkdir()
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


def test_env_match_passes_for_a_venv_uv_says_matches_the_lock(tmp_path, monkeypatch):
    make_campaign(tmp_path)
    pretend_uv_check(monkeypatch)
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


def test_env_match_refuses_a_venv_uv_says_disagrees_with_the_lock(
    tmp_path, monkeypatch
):
    """The lock was bumped and the venv not rebuilt, or the venv was pip-installed
    into. One check covers both, because uv is asked about the environment rather
    than about a record of how it was built."""
    make_campaign(tmp_path)
    pretend_uv_check(monkeypatch, ok=False, detail="Would install: requests==2.32.5")
    pretend_running_in(monkeypatch, campaign_mod.venv_path(tmp_path, "nprep"))
    with pytest.raises(SystemExit) as e:
        campaign_mod.require_env_match(tmp_path, "nprep")
    message = str(e.value)
    assert "campaign update-env" in message
    # uv's own words survive as evidence, under our explanation rather than instead
    # of it: a bare "Would install: X" does not say which of the user's two
    # environments is wrong, nor which command fixes it.
    assert "Would install: requests==2.32.5" in message


def test_env_match_accepts_a_venv_mechababs_did_not_build(tmp_path, monkeypatch):
    """What unblocks detached reproduction: the guard asks uv whether this
    environment matches this lock, not whether `campaign init` built it. A venv
    somebody built themselves from the committed lock is exactly the venv a
    re-runner has. Provenance is not weakened by it — two venvs built from one lock
    hold the same tools, and it is the tools a run is attributed to.
    """
    make_campaign(tmp_path)
    pretend_uv_check(monkeypatch)  # uv: this environment matches the lock
    pretend_running_in(monkeypatch, campaign_mod.venv_path(tmp_path, "nprep"))
    assert campaign_mod.require_env_match(tmp_path, "nprep")


def test_env_match_refuses_a_campaign_with_no_lock(tmp_path, monkeypatch):
    """Before uv is asked anything: with no lock there is nothing to compare against,
    and `--frozen` would fail with uv's words for a mechababs-shaped mistake."""
    make_campaign(tmp_path)
    campaign_mod.uv_lock_path(tmp_path, "nprep").unlink()
    pretend_running_in(monkeypatch, campaign_mod.venv_path(tmp_path, "nprep"))
    with pytest.raises(SystemExit, match="uv.lock"):
        campaign_mod.require_env_match(tmp_path, "nprep")


# --- what the guard actually asks uv ----------------------------------------


def test_the_freshness_check_is_a_frozen_offline_uv_check_of_this_interpreter(
    tmp_path, monkeypatch
):
    """The invocation is load-bearing in four ways, so it is pinned as one.

    `--check` reports instead of installing (a guard must not mutate what it
    vouches for); `--frozen` forbids re-resolving, so the guard can never chase a
    moved branch pin or rewrite the lock it is checking; `--offline` keeps it
    network-free; and UV_PROJECT_ENVIRONMENT names *this* interpreter, without
    which uv checks the project's own .venv and warns rather than failing — which at
    a member is not the venv doing the work.
    """
    import subprocess

    seen = {}

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env", {})
        return Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "prefix", "/some/campaign/.venv")

    ok, _ = campaign_mod.venv_matches_lock(tmp_path / "campaign")

    assert ok
    assert seen["cmd"] == [
        "/some/campaign/.venv/bin/uv",
        "sync",
        "--check",
        "--frozen",
        "--offline",
        "--project",
        str(tmp_path / "campaign"),
    ]
    assert seen["env"]["UV_PROJECT_ENVIRONMENT"] == "/some/campaign/.venv"


def test_uv_is_resolved_from_this_venv_and_never_from_path(monkeypatch):
    """Same rule as babs_bin, same reason: PATH can disagree with the pin, and the
    check has to work where nothing was activated for it (a rerun in a clone)."""
    monkeypatch.setattr(sys, "prefix", "/campaigns/nprep/.venv")
    assert campaign_mod.uv_bin() == "/campaigns/nprep/.venv/bin/uv"


def test_a_failed_check_reports_uvs_output_and_a_pass_reports_the_code(
    tmp_path, monkeypatch
):
    import subprocess

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "Would uninstall: six==1.17.0\n"

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: Failed())
    ok, detail = campaign_mod.venv_matches_lock(tmp_path)
    assert not ok
    assert detail == "Would uninstall: six==1.17.0"


def test_require_selected_campaign_bundles_the_three_preconditions(
    tmp_path, monkeypatch
):
    (tmp_path / ".datalad").mkdir()
    make_campaign(tmp_path)
    pretend_uv_check(monkeypatch)
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

    A relative marker could not express this: `..` resolves against wherever the
    clone now sits, so the member would silently adopt an unrelated parent directory
    as the place its environment lives.
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


def test_a_detached_member_still_refuses_to_be_operated_from(tmp_path, monkeypatch):
    """Losing sight of the superstudy does not turn a member into a lone study.

    The campaign was configured at a superstudy and its marker still says so; that
    the super is out of reach changes only where it *is*, not what the campaign is.
    Advancing here would produce cells the super's catalog never hears about — and
    unreachability is precisely when nothing would notice. So the level check asks
    the marker's PRESENCE, while `operated_level` asks its RESOLUTION.
    """
    from mechababs import campaign as c

    member = tmp_path / "elsewhere" / "study-ds000001"
    c.campaign_dir(member, "nprep").mkdir(parents=True)
    # A marker naming a superstudy that is nowhere above this clone.
    c.config_path(member, "nprep").write_text(
        "label: nprep\nsuperstudy: 99999999-8888-7777-6666-555555555555\n"
    )
    (member / ".datalad").mkdir()
    monkeypatch.setenv(c.CAMPAIGN_ENV_VAR, "nprep")

    assert c.superstudy_of(member, "nprep") is None, "the super must be unfindable"

    with pytest.raises(SystemExit) as excinfo:
        c.require_selected_campaign(str(member))
    message = str(excinfo.value)
    assert "operated from its superstudy" in message
    # It cannot name a directory, so it names the id rather than going quiet.
    assert "99999999-8888-7777-6666-555555555555" in message


def test_a_member_is_never_operated_from_and_there_is_no_override(
    tmp_path, monkeypatch
):
    """The refusal is unconditional: no override for advancing a member detached from
    its superstudy. What a detached member supports is *reproduction*: `datalad rerun` of
    its own recorded commands, which carry their own study-local env check and never
    pass through here. Advancing is the part that must not happen detached, because
    the cells it would add are ones the superstudy's catalog never hears about.
    """
    import inspect

    from mechababs import campaign as c

    member = tmp_path / "study-ds000001"
    c.campaign_dir(member, "nprep").mkdir(parents=True)
    c.config_path(member, "nprep").write_text(
        f"label: nprep\nsuperstudy: {stamp_dataset_id(tmp_path)}\n"
    )
    (member / ".datalad").mkdir()
    monkeypatch.setenv(c.CAMPAIGN_ENV_VAR, "nprep")

    with pytest.raises(SystemExit, match="operated from its superstudy"):
        c.require_selected_campaign(str(member))
    assert (
        "allow_member" not in inspect.signature(c.require_selected_campaign).parameters
    )


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
    pretend_uv_check(monkeypatch)
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


def test_env_match_at_a_member_checks_against_the_canonical_lock(tmp_path, monkeypatch):
    """The outer guard asks about the CANONICAL lock even when called at a member.

    Which lock is compared is the whole division of labour between the two checks:
    the outer guard proves the venv is the superstudy's current environment, and the
    inner verbs then ask the member's own copy. Pointing this one at the member
    instead would collapse the two and leave nothing checking the canonical lock at
    all — the member's copy would vouch for itself.
    """
    make_campaign(tmp_path)
    member = make_member(tmp_path)
    asked = {}
    monkeypatch.setattr(
        campaign_mod,
        "venv_matches_lock",
        lambda campaign: (asked.setdefault("campaign", campaign), (True, ""))[1],
    )
    pretend_running_in(monkeypatch, campaign_mod.venv_path(tmp_path, "nprep"))

    campaign_mod.require_env_match(member, "nprep")
    assert asked["campaign"] == campaign_mod.campaign_dir(tmp_path, "nprep")


def test_env_match_at_a_member_still_refuses_when_uv_says_the_venv_drifted(
    tmp_path, monkeypatch
):
    """Resolving at the operated level must not weaken the drift check."""
    make_campaign(tmp_path)
    member = make_member(tmp_path)
    pretend_uv_check(monkeypatch, ok=False, detail="Would install: babs==0.5.5")
    pretend_running_in(monkeypatch, campaign_mod.venv_path(tmp_path, "nprep"))

    with pytest.raises(SystemExit) as e:
        campaign_mod.require_env_match(member, "nprep")
    assert "campaign update-env" in str(e.value)


# --- the inner verbs' check: this venv vs THIS STUDY's lock -----------------


def test_the_study_lock_check_passes_when_uv_says_the_venv_matches(
    tmp_path, monkeypatch
):
    make_campaign(tmp_path)
    pretend_uv_check(monkeypatch)
    assert campaign_mod.require_study_lock_match(
        tmp_path, "nprep"
    ) == campaign_mod.campaign_dir(tmp_path, "nprep")


def test_the_study_lock_check_asks_about_the_study_it_stands_in_not_the_super(
    tmp_path, monkeypatch
):
    """The counterpart of the guard test above, and the member-drift gate itself.

    On the dispatched path the outer guard has already proved venv = canonical lock,
    so aiming this at the member is what makes a lagging copy fail — aimed at the
    super it would agree with the check that just ran and gate nothing.
    """
    make_campaign(tmp_path)
    member = make_member(tmp_path)
    asked = {}
    monkeypatch.setattr(
        campaign_mod,
        "venv_matches_lock",
        lambda campaign: (asked.setdefault("campaign", campaign), (True, ""))[1],
    )

    campaign_mod.require_study_lock_match(member, "nprep")
    assert asked["campaign"] == campaign_mod.campaign_dir(member, "nprep")


def test_the_study_lock_check_does_not_care_which_venv_directory_it_is(
    tmp_path, monkeypatch
):
    """No location check, deliberately: a rerun in a cloned study runs a venv the
    re-runner built, at a path mechababs has never heard of. Two venvs built from
    one lock hold the same tools, so the lock answers everything that matters."""
    make_campaign(tmp_path)
    pretend_uv_check(monkeypatch)
    pretend_running_in(monkeypatch, tmp_path / "somewhere" / "else" / ".venv")

    assert campaign_mod.require_study_lock_match(tmp_path, "nprep")


def test_a_lone_study_whose_venv_drifted_is_told_to_rebuild_from_its_own_lock(
    tmp_path, monkeypatch
):
    make_campaign(tmp_path)
    pretend_uv_check(monkeypatch, ok=False, detail="Would install: babs==0.5.5")

    with pytest.raises(SystemExit) as e:
        campaign_mod.require_study_lock_match(tmp_path, "nprep")
    message = str(e.value)
    assert "uv sync --frozen" in message
    # a lone study has no superstudy to be told to go to
    assert "--study" not in message
    assert "Would install: babs==0.5.5" in message


def test_a_drifted_member_is_told_to_acknowledge_it_at_the_superstudy(
    tmp_path, monkeypatch
):
    """The refuse-don't-refresh rule, in the one message that has to carry it.

    A member behind the canonical lock is refused, never auto-refreshed: advancing
    it would write run records into a study whose committed lock names other tools.
    Moving its remaining work onto the new environment is a human act, and the
    message names the command that records it — at the superstudy, since that is
    where the environment lives.
    """
    make_campaign(tmp_path)
    member = make_member(tmp_path)
    pretend_uv_check(monkeypatch, ok=False, detail="Would install: babs==0.5.5")

    with pytest.raises(SystemExit) as e:
        campaign_mod.require_study_lock_match(member, "nprep")
    message = str(e.value)
    assert "mechababs campaign update-env --study study-ds000001" in message
    # never "we refreshed it for you", and never the lone-study advice
    assert "uv sync --frozen" not in message


def test_the_member_hint_appears_only_for_a_member(tmp_path, monkeypatch):
    """Gated on the superstudy marker, not on the failure: a lone study told to run
    `update-env --study` would be sent to a level that does not exist."""
    make_campaign(tmp_path)
    lone = tmp_path / "study-ds000002"
    campaign_mod.campaign_dir(lone, "nprep").mkdir(parents=True)
    campaign_mod.config_path(lone, "nprep").write_text("label: nprep\n")
    campaign_mod.uv_lock_path(lone, "nprep").write_text("lock-v1\n")
    pretend_uv_check(monkeypatch, ok=False)

    with pytest.raises(SystemExit) as e:
        campaign_mod.require_study_lock_match(lone, "nprep")
    assert "superstudy" not in str(e.value)


def test_the_study_lock_check_refuses_a_study_carrying_no_lock(tmp_path):
    """Every study carries the lock of the campaign working in it — that copy is what
    a rerun rebuilds from, so its absence is a broken footprint, not a drift."""
    make_campaign(tmp_path)
    member = make_member(tmp_path)
    campaign_mod.uv_lock_path(member, "nprep").unlink()

    with pytest.raises(SystemExit, match="uv.lock"):
        campaign_mod.require_study_lock_match(member, "nprep")
