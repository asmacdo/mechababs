"""Unit tests for `mechababs retire-derivative <path> (--remove | --path DEST)`.

The fixture studies are hand-built trees — no datalad, no cluster — because
everything retire decides is decided from files: which study the path names, which
cell claims the derivative, whether the destination is outside, and which attempt
number is free. The one step that reaches outside the process (the `datalad save`)
is stubbed; that it is *asked for*, per level and path-scoped, is asserted.

The move itself is NOT stubbed. Whether the derivative is really gone from the study
and really present at the destination — and whether an absorbed git directory came
with it — is the half a mock would hide, so it runs for real on the tmp tree.
"""

import subprocess
import sys
from contextlib import contextmanager

import pytest
import yaml

from conftest import pretend_uv_check, stamp_dataset_id
from mechababs import campaign as campaign_mod
from mechababs import retire

APP = "bids-app-configs/MRIQC-24.0.2.yaml"
DERIVATIVE = "derivatives/MRIQC-24.0.2+ds000001+nprep"

# One scaffolded, merged cell — the state a derivative that has to be redone is
# actually in. Identity and topology are filled so the reset can be shown to leave
# them alone.
CELL = {
    "source_dataset": "sourcedata/ds000001",
    "app_config": APP,
    "processing_level": "subject",
    "n_subjects": "2",
    "n_sessions": "",
    "depends_on": "",
    "babs": DERIVATIVE,
    "merged": "true",
}


def _make_campaign(root, *, superstudy=False, rows=None, owner=None):
    """Write a campaign footprint at ``root``: config, shard-or-catalog, lock, venv."""
    cdir = campaign_mod.campaign_dir(root, "nprep")
    (cdir / campaign_mod.APPS_DIRNAME).mkdir(parents=True)
    (campaign_mod.apps_dir(root, "nprep") / "MRIQC-24.0.2.yaml").write_text(
        "bids_app_args: {}\n"
    )
    config = {
        "label": "nprep",
        "apps": [APP],
        "cluster": "clusters/dartmouth.yaml",
        "limit": None,
    }
    if owner:
        config[campaign_mod.SUPERSTUDY_KEY] = owner
    campaign_mod.config_path(root, "nprep").write_text(yaml.safe_dump(config))
    if superstudy:
        campaign_mod.members_path(root, "nprep").write_text(
            campaign_mod.initial_members_header()
        )
    else:
        campaign_mod.state_path(root, "nprep").write_text(campaign_mod.initial_header())
        campaign_mod.write_state(root, "nprep", rows if rows is not None else [CELL])
    campaign_mod.uv_lock_path(root, "nprep").write_text("lock-v1\n")
    return cdir


def _make_derivative(study, rel=DERIVATIVE):
    """A derivative on disk with a real `.git` DIRECTORY — datalad's normal shape."""
    path = study / rel
    (path / ".git").mkdir(parents=True)
    (path / "dataset_description.json").write_text("{}\n")
    (path / "logs").mkdir()
    (path / "logs" / "job.o").write_text("the evidence\n")
    return path


def _select(root, monkeypatch):
    """Stand at ``root`` with campaign 'nprep' selected and the env guard satisfied."""
    venv = campaign_mod.venv_path(root, "nprep")
    venv.mkdir(exist_ok=True)
    pretend_uv_check(monkeypatch)
    monkeypatch.setenv(campaign_mod.CAMPAIGN_ENV_VAR, "nprep")
    monkeypatch.setattr("sys.prefix", str(venv))
    monkeypatch.chdir(root)


@pytest.fixture
def study(tmp_path, monkeypatch):
    """A lone study with campaign 'nprep' and one merged, scaffolded cell."""
    root = tmp_path / "study-ds000001"
    (root / ".datalad").mkdir(parents=True)
    _make_campaign(root)
    _make_derivative(root)
    _select(root, monkeypatch)
    return root


@pytest.fixture
def superstudy(tmp_path, monkeypatch):
    """A superstudy configured at the super, with one member carrying the cell."""
    root = tmp_path / "my-super"
    (root / ".datalad").mkdir(parents=True)
    owner = stamp_dataset_id(root)
    _make_campaign(root, superstudy=True)

    member = root / "study-ds000001"
    (member / ".datalad").mkdir(parents=True)
    _make_campaign(member, owner=owner)
    _make_derivative(member)

    _select(root, monkeypatch)
    return root, member


@pytest.fixture
def saves(monkeypatch):
    """Stub the save scope; record (root, message, paths) per level, in order.

    The fixture studies are plain directories, so the real scope (a datalad status
    plus a save) is replaced by a null scope that still honours the contract: it
    yields a PendingSave and requires a message on exit.
    """
    calls = []

    @contextmanager
    def null_scope(root, paths):
        pending = retire.utils.PendingSave()
        yield pending
        assert pending.message, "scope exited with no message set"
        calls.append((root, pending.message, paths))

    monkeypatch.setattr(retire.utils, "campaign_save_scope", null_scope)
    return calls


def _rows(study):
    return campaign_mod.read_state(study, "nprep")


# --- naming the derivative ---------------------------------------------------


def test_a_relative_path_is_taken_from_the_campaign_root(study):
    study_rel, derivative = retire.parse_derivative_path(study, DERIVATIVE)
    assert (str(study_rel), derivative) == (".", DERIVATIVE)


def test_an_absolute_path_is_accepted(study):
    study_rel, derivative = retire.parse_derivative_path(study, study / DERIVATIVE)
    assert (str(study_rel), derivative) == (".", DERIVATIVE)


def test_a_member_qualified_path_names_the_member(superstudy):
    root, _ = superstudy
    study_rel, derivative = retire.parse_derivative_path(
        root, f"study-ds000001/{DERIVATIVE}"
    )
    assert (str(study_rel), derivative) == ("study-ds000001", DERIVATIVE)


def test_a_path_not_under_derivatives_is_refused(study):
    with pytest.raises(SystemExit) as excinfo:
        retire.parse_derivative_path(study, "sourcedata/ds000001")
    assert "not a derivative path" in str(excinfo.value)


def test_a_path_outside_the_campaign_root_is_refused(study, tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        retire.parse_derivative_path(study, tmp_path / "elsewhere" / DERIVATIVE)
    assert "is not inside" in str(excinfo.value)


# --- the configured-level rule, both directions ------------------------------


def test_a_member_qualified_path_is_refused_at_a_lone_study(study, saves):
    """A study-configured campaign has no member to name."""
    with pytest.raises(SystemExit) as excinfo:
        retire.run_retire(f"somewhere/{DERIVATIVE}", remove=True)
    assert "configured at a study" in str(excinfo.value)


def test_a_bare_path_is_refused_at_a_superstudy(superstudy, saves):
    """A super-configured campaign holds no cells of its own — name the member."""
    root, _ = superstudy
    with pytest.raises(SystemExit) as excinfo:
        retire.run_retire(DERIVATIVE, remove=True)
    assert "configured at a superstudy" in str(excinfo.value)


def test_a_member_without_this_campaign_is_refused(superstudy, saves):
    root, _ = superstudy
    (root / "study-other" / ".datalad").mkdir(parents=True)
    with pytest.raises(SystemExit) as excinfo:
        retire.run_retire(f"study-other/{DERIVATIVE}", remove=True)
    assert "no campaign 'nprep' here" in str(excinfo.value)


# --- the flags: exactly one, never both --------------------------------------
#
# Driven through the real CLI, because "exactly one of these two" is an
# argparse-level fact: asserting it on the function alone would test the assert,
# not the interface a user meets.


def _cli(argv, monkeypatch):
    from mechababs import cli

    monkeypatch.setattr(sys, "argv", ["mechababs", "retire-derivative", *argv])
    return cli.main()


def test_neither_flag_is_refused(monkeypatch, capsys):
    with pytest.raises(SystemExit) as excinfo:
        _cli([DERIVATIVE], monkeypatch)
    assert excinfo.value.code == 2
    assert "one of the arguments --path --remove is required" in capsys.readouterr().err


def test_both_flags_are_refused(monkeypatch, capsys):
    with pytest.raises(SystemExit) as excinfo:
        _cli([DERIVATIVE, "--remove", "--path", "/tmp/attic"], monkeypatch)
    assert excinfo.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_the_cli_hands_each_flag_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        retire,
        "run_retire",
        lambda path, *, dest=None, remove=False, root=".": (
            seen.update(path=path, dest=dest, remove=remove) or 0
        ),
    )
    _cli([DERIVATIVE, "--path", "/tmp/attic"], monkeypatch)
    assert seen == {"path": DERIVATIVE, "dest": "/tmp/attic", "remove": False}
    _cli([DERIVATIVE, "--remove"], monkeypatch)
    assert seen == {"path": DERIVATIVE, "dest": None, "remove": True}


# --- the destination has to be outside ---------------------------------------


def test_a_destination_inside_the_study_is_refused(study, saves, tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        retire.run_retire(DERIVATIVE, dest=str(study / "attic"))
    assert "must be outside the study" in str(excinfo.value)
    assert (study / DERIVATIVE).is_dir(), "the derivative moved despite the refusal"


def test_a_destination_reaching_back_in_is_refused(study, saves):
    """Resolved, not string-compared: `..` and symlinks both have to be caught."""
    with pytest.raises(SystemExit) as excinfo:
        retire.run_retire(DERIVATIVE, dest=str(study / "sourcedata" / ".." / "attic"))
    assert "must be outside the study" in str(excinfo.value)


def test_the_study_itself_is_refused_as_a_destination(study, saves):
    with pytest.raises(SystemExit) as excinfo:
        retire.run_retire(DERIVATIVE, dest=str(study))
    assert "must be outside the study" in str(excinfo.value)


def test_a_destination_inside_the_superstudy_is_refused(superstudy, saves):
    """Outside the member is not enough — the superstudy is a published object too."""
    root, _ = superstudy
    with pytest.raises(SystemExit) as excinfo:
        retire.run_retire(f"study-ds000001/{DERIVATIVE}", dest=str(root / "attic"))
    assert "must be outside the study" in str(excinfo.value)


def test_a_destination_outside_both_is_accepted(superstudy, saves, tmp_path):
    root, member = superstudy
    retire.run_retire(f"study-ds000001/{DERIVATIVE}", dest=str(tmp_path / "attic"))
    assert not (member / DERIVATIVE).exists()


# --- the archive ---------------------------------------------------------------


def test_the_archive_is_named_for_the_study_and_the_derivative(study, saves, tmp_path):
    retire.run_retire(DERIVATIVE, dest=str(tmp_path / "attic"))
    parked = tmp_path / "attic" / "study-ds000001-MRIQC-24.0.2+ds000001+nprep-attempt-1"
    assert parked.is_dir(), sorted(p.name for p in (tmp_path / "attic").iterdir())
    assert (parked / "logs" / "job.o").read_text() == "the evidence\n"
    assert not (study / DERIVATIVE).exists(), "the derivative is still in the study"


def test_attempt_numbers_take_the_first_free_slot(study, saves, tmp_path):
    """Never clobbers: the same cell retired twice lands beside its predecessor."""
    attic = tmp_path / "attic"
    for taken in ("attempt-1", "attempt-2"):
        (attic / f"study-ds000001-MRIQC-24.0.2+ds000001+nprep-{taken}").mkdir(
            parents=True
        )

    retire.run_retire(DERIVATIVE, dest=str(attic))

    parked = attic / "study-ds000001-MRIQC-24.0.2+ds000001+nprep-attempt-3"
    assert (parked / "logs" / "job.o").is_file()


def test_one_dest_collects_attempts_from_two_studies(tmp_path, saves, monkeypatch):
    """The study name is the prefix, so two studies retiring the same app do not
    collide on one path — the job the dataset id did when the archive lived inside a
    campaign."""
    attic = tmp_path / "attic"
    for name in ("study-ds000001", "study-ds000002"):
        root = tmp_path / name
        (root / ".datalad").mkdir(parents=True)
        _make_campaign(root)
        _make_derivative(root)
        _select(root, monkeypatch)
        retire.run_retire(DERIVATIVE, dest=str(attic))

    assert sorted(p.name for p in attic.iterdir()) == [
        "study-ds000001-MRIQC-24.0.2+ds000001+nprep-attempt-1",
        "study-ds000002-MRIQC-24.0.2+ds000001+nprep-attempt-1",
    ]


def test_remove_deletes_the_derivative_outright(study, saves, tmp_path):
    retire.run_retire(DERIVATIVE, remove=True)
    assert not (study / DERIVATIVE).exists()
    assert not list(tmp_path.glob("**/job.o")), "--remove parked the evidence somewhere"


def test_a_cross_filesystem_archive_copies_and_then_deletes(study, saves, monkeypatch):
    """A cluster DEST on a different mount than the study is the normal case.

    There is no rename across filesystems, so the tree is copied and the original
    deleted — with our own annex-aware delete, because `shutil.move`'s cross-device
    fallback ends in a plain `rmtree` that dies on the read-only object store. Forced
    here by making `os.rename` raise the way EXDEV does, since a second filesystem is
    not something a unit test can conjure.
    """
    attic = study.parent / "attic"
    objects = study / DERIVATIVE / ".git" / "annex" / "objects"
    objects.mkdir(parents=True)
    (objects / "MD5E-s1450--deadbeef.yaml").write_text("annexed content\n")
    (objects / "MD5E-s1450--deadbeef.yaml").chmod(0o444)
    objects.chmod(0o555)
    monkeypatch.setattr(
        retire.os, "rename", lambda *a, **k: (_ for _ in ()).throw(OSError("EXDEV"))
    )

    retire.run_retire(DERIVATIVE, dest=str(attic))

    parked = attic / "study-ds000001-MRIQC-24.0.2+ds000001+nprep-attempt-1"
    assert (parked / "logs" / "job.o").read_text() == "the evidence\n"
    assert (parked / ".git" / "annex" / "objects").is_dir(), "the annex did not travel"
    assert not (study / DERIVATIVE).exists(), "the original was left behind"


def test_remove_gets_through_a_read_only_annex_object_store(study, saves):
    """git-annex takes the write bit off its object files *and* the directories that
    hold them, which is how it protects content — and it makes a plain `shutil.rmtree`
    die with EACCES on the first annexed object. Found by the e2e against a real babs
    derivative; kept here so it stays found without one.

    `--path` needs none of this: a move is a rename and never touches the contents.
    """
    objects = study / DERIVATIVE / ".git" / "annex" / "objects" / "pF" / "Jk"
    objects.mkdir(parents=True)
    (objects / "MD5E-s1450--deadbeef.yaml").write_text("annexed content\n")
    (objects / "MD5E-s1450--deadbeef.yaml").chmod(0o444)
    for readonly in (objects, objects.parent, objects.parent.parent):
        readonly.chmod(0o555)

    retire.run_retire(DERIVATIVE, remove=True)

    assert not (study / DERIVATIVE).exists()


# --- the cell reset ------------------------------------------------------------


def test_the_derived_columns_are_blanked_and_identity_is_untouched(
    study, saves, tmp_path
):
    retire.run_retire(DERIVATIVE, dest=str(tmp_path / "attic"))

    (row,) = _rows(study)
    assert row["babs"] == "" and row["merged"] == "", row
    for column in campaign_mod.IDENTITY_COLUMNS + campaign_mod.TOPOLOGY_COLUMNS:
        assert row[column] == CELL[column], f"{column} was rewritten by the reset"


def test_only_the_retired_cell_is_reset(tmp_path, saves, monkeypatch):
    """A sibling cell in the same shard is left exactly as it was."""
    root = tmp_path / "study-ds000001"
    (root / ".datalad").mkdir(parents=True)
    sibling = {
        **CELL,
        "app_config": "bids-app-configs/fMRIPrep-25.2.5+anat.yaml",
        "babs": "derivatives/fMRIPrep-25.2.5+anat+ds000001+nprep",
    }
    _make_campaign(root, rows=[CELL, sibling])
    _make_derivative(root)
    _select(root, monkeypatch)

    retire.run_retire(DERIVATIVE, dest=str(tmp_path / "attic"))

    retired, untouched = _rows(root)
    assert retired["babs"] == ""
    assert untouched["babs"] == sibling["babs"] and untouched["merged"] == "true"


def test_a_derivative_no_cell_claims_is_refused(tmp_path, saves, monkeypatch):
    root = tmp_path / "study-ds000001"
    (root / ".datalad").mkdir(parents=True)
    _make_campaign(root, rows=[{**CELL, "babs": "", "merged": ""}])
    _make_derivative(root)
    _select(root, monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        retire.run_retire(DERIVATIVE, dest=str(tmp_path / "attic"))
    assert "no cell in this campaign's statefile claims" in str(excinfo.value)


def test_a_refused_derivative_is_not_moved_first(tmp_path, saves, monkeypatch):
    """The claim is checked BEFORE the transaction opens, so a refusal cannot leave
    the derivative detached and the shard untouched — the half-retired state doing
    both in one scope exists to prevent."""
    root = tmp_path / "study-ds000001"
    (root / ".datalad").mkdir(parents=True)
    _make_campaign(root, rows=[{**CELL, "babs": "", "merged": ""}])
    _make_derivative(root)
    _select(root, monkeypatch)

    with pytest.raises(SystemExit):
        retire.run_retire(DERIVATIVE, dest=str(tmp_path / "attic"))

    assert (root / DERIVATIVE / "logs" / "job.o").is_file()
    assert not (tmp_path / "attic").exists()
    assert saves == [], "a scope was opened for a retirement that was refused"


def test_a_missing_derivative_is_refused(study, saves, tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        retire.run_retire("derivatives/never-existed", dest=str(tmp_path / "attic"))
    assert "no such derivative" in str(excinfo.value)


# --- what each level commits ---------------------------------------------------


def test_a_lone_study_commits_once_declaring_three_paths(study, saves, tmp_path):
    retire.run_retire(DERIVATIVE, dest=str(tmp_path / "attic"))

    (root, message, paths) = saves[0]
    assert len(saves) == 1, "a lone study has no second level to commit at"
    assert root == study
    assert paths == [
        DERIVATIVE,
        ".gitmodules",
        str(campaign_mod.state_path(study, "nprep").relative_to(study)),
    ]
    assert message.startswith(f"mechababs retire-derivative {DERIVATIVE} --path ")


def test_remove_says_so_in_the_message(study, saves):
    retire.run_retire(DERIVATIVE, remove=True)
    assert saves[0][1] == (
        f"mechababs retire-derivative {DERIVATIVE} --remove (campaign 'nprep')"
    )


def test_the_member_commits_its_facts_and_the_super_commits_the_gitlink(
    superstudy, saves, tmp_path
):
    """Every level stays clean, and neither commits the other's work."""
    root, member = superstudy
    retire.run_retire(f"study-ds000001/{DERIVATIVE}", dest=str(tmp_path / "attic"))

    # The member's scope closes first (it is the inner one), so it is recorded first.
    (member_root, _, member_paths), (super_root, _, super_paths) = saves
    assert member_root == member
    assert member_paths == [
        DERIVATIVE,
        ".gitmodules",
        str(campaign_mod.state_path(member, "nprep").relative_to(member)),
    ]
    assert super_root == root
    assert super_paths == member, "the super declares the member, and nothing else"


def test_the_supers_scope_opens_before_the_members(superstudy, monkeypatch, tmp_path):
    """The super's clean-in has to run while the member is still clean — the other
    way round it would see its own intended change as pre-existing dirt."""
    opened = []

    @contextmanager
    def watching_scope(root, paths):
        opened.append(root)
        pending = retire.utils.PendingSave()
        yield pending
        assert pending.message

    monkeypatch.setattr(retire.utils, "campaign_save_scope", watching_scope)
    root, member = superstudy
    retire.run_retire(f"study-ds000001/{DERIVATIVE}", dest=str(tmp_path / "attic"))
    assert opened == [root, member]


def test_membership_is_not_touched(superstudy, saves, tmp_path):
    """The cell is back at the start, not unselected: the catalog does not change."""
    root, _ = superstudy
    before = campaign_mod.members_path(root, "nprep").read_text()
    retire.run_retire(f"study-ds000001/{DERIVATIVE}", dest=str(tmp_path / "attic"))
    assert campaign_mod.members_path(root, "nprep").read_text() == before


# --- absorbed git directories ---------------------------------------------------
#
# datalad leaves a subdataset's `.git` a real DIRECTORY, which is what makes the move
# a relocation. `git submodule absorbgitdirs` does not, and a tree moved in that
# state is dead — the `gitdir:` pointer resolves to nothing from the new location.
# The e2e cannot produce one, so it is built here with real git.


def _absorbed_super(tmp_path):
    """A git repo with a submodule whose git dir has been absorbed into the parent."""
    parent = tmp_path / "parent"
    sub = tmp_path / "sub"
    for path in (parent, sub):
        path.mkdir()
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        (path / "f.txt").write_text("x\n")
        subprocess.run(["git", "-C", str(path), "add", "f.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-qm",
                "seed",
            ],
            check=True,
        )
    subprocess.run(
        [
            "git",
            "-C",
            str(parent),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "-q",
            "add",
            str(sub),
            "derivatives/X",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(parent), "submodule", "absorbgitdirs"], check=True)
    return parent, parent / "derivatives" / "X"


def test_a_real_git_directory_is_left_alone(study):
    assert retire.absorbed_gitdir(study / DERIVATIVE) is None


def test_an_absorbed_git_directory_is_found(tmp_path):
    parent, derivative = _absorbed_super(tmp_path)
    assert (derivative / ".git").is_file(), "git did not absorb the submodule"
    assert (
        retire.absorbed_gitdir(derivative)
        == (parent / ".git" / "modules" / "derivatives" / "X").resolve()
    )


def test_an_absorbed_derivative_is_a_working_repository_after_the_move(tmp_path):
    """The whole point: what lands at DEST has to be readable, or the archive is a
    directory of files with no history — and the history is the evidence."""
    parent, derivative = _absorbed_super(tmp_path)
    dest = tmp_path / "attic" / "parked"

    retire.detach(parent, "derivatives/X", dest)

    assert (dest / ".git").is_dir(), "the git dir did not come home"
    log = subprocess.run(
        ["git", "-C", str(dest), "log", "-1", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert log == "seed"
    assert not subprocess.run(
        ["git", "-C", str(dest), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip(), "core.worktree still points at the old parent"


def test_remove_drops_an_absorbed_git_directory_too(tmp_path):
    """Else the worktree goes and the whole repository stays behind as cruft."""
    parent, derivative = _absorbed_super(tmp_path)
    absorbed = retire.absorbed_gitdir(derivative)

    retire.detach(parent, "derivatives/X", None)

    assert not derivative.exists()
    assert not absorbed.exists(), f"{absorbed} left behind"
