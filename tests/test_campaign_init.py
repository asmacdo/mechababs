"""Unit tests for `mechababs campaign init`.

Fast tests stub the two slow, outside-world steps — the uv env build and the
datalad save — and assert on the campaign's *contents*: the copied configs, the
pins written into pyproject.toml, the header-only statefile, and the select +
activate script. The real env build is exercised by the `uv_build` integration
test at the foot of this file.
"""

import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from mechababs import campaign as campaign_mod
from mechababs import campaign_init, utils


@pytest.fixture
def study(tmp_path):
    """A study root, minimal: mechababs only requires it to be a dataset."""
    root = tmp_path / "study-ds000001"
    (root / ".datalad").mkdir(parents=True)
    return root


@pytest.fixture
def configs(tmp_path):
    """The user's own app + cluster configs, somewhere outside the study."""
    src = tmp_path / "my-configs"
    src.mkdir()
    (src / "MRIQC-24.0.2.yaml").write_text("bids_app_args: {}\n")
    (src / "fMRIPrep-25.2.5+anat.yaml").write_text("bids_app_args: {}\n")
    (src / "fMRIPrep-25.2.5+minimal.yaml").write_text(
        "mechababs:\n  depends_on: fMRIPrep-25.2.5+anat\n"
    )
    (src / "dartmouth.yaml").write_text("cluster_resources: {}\n")
    (src / "old-glibc.yaml").write_text(
        "cluster_resources: {}\nenv_constraints:\n  - pandas<=2.3.2\n  - h5py<=3.14.0\n"
    )
    return src


def pretend_build_env(campaign, cluster_file=None):
    """What `build_env` leaves behind, without running uv: a venv and a lock.

    Nothing else — there is no third artifact to fake. mechababs records nothing
    about an environment, so "was this venv built from this lock" is a question only
    uv answers, from the two files themselves.
    """
    venv = campaign / campaign_mod.VENV_DIRNAME
    venv.mkdir()
    (campaign / campaign_mod.UV_LOCK_FILENAME).write_text("# resolved\n")
    return venv


@pytest.fixture
def stub_env(monkeypatch):
    """Stub the env build + datalad save; record that each was asked for.

    Both reach outside the process (uv resolves over the network; datalad commits
    in a real dataset), and neither is what these tests are about.
    """
    calls = {}

    def fake_build_env(campaign, cluster_file):
        calls["build_env"] = (campaign, cluster_file)
        return pretend_build_env(campaign)

    @contextmanager
    def fake_save_scope(root, path):
        pending = utils.PendingSave()
        yield pending
        calls["save"] = (root, pending.message, path)

    monkeypatch.setattr(campaign_init, "build_env", fake_build_env)
    monkeypatch.setattr(campaign_init, "campaign_save_scope", fake_save_scope)
    return calls


def init(study, configs, *names, label="nprep", cluster="dartmouth", **kwargs):
    apps = [str(configs / f"{n}.yaml") for n in names] or [
        str(configs / "MRIQC-24.0.2.yaml")
    ]
    return campaign_init.init(
        study, label, apps, str(configs / f"{cluster}.yaml"), **kwargs
    )


# --- the campaign's contents ------------------------------------------------


def test_init_writes_the_campaign_layout(study, configs, stub_env):
    campaign = init(study, configs, "MRIQC-24.0.2", "fMRIPrep-25.2.5+anat")

    assert campaign == study / ".mechababs" / "campaigns" / "nprep"
    for name in (
        campaign_mod.CONFIG_FILENAME,
        campaign_mod.STATE_FILENAME,
        campaign_mod.ENV_FILENAME,
        campaign_mod.PYPROJECT_FILENAME,
    ):
        assert (campaign / name).is_file(), name
    # the configs are COPIED in — the run reproduces from the study alone
    assert (campaign / "bids-app-configs" / "MRIQC-24.0.2.yaml").is_file()
    assert (campaign / "clusters" / "dartmouth.yaml").is_file()


def test_the_statefile_is_header_only(study, configs, stub_env):
    # which source datasets a campaign acts on is add-dataset's explicit selection
    campaign = init(study, configs)
    assert (
        campaign / campaign_mod.STATE_FILENAME
    ).read_text() == campaign_mod.initial_header()


def test_campaign_yaml_records_the_bundle_order_cluster_and_limit(
    study, configs, stub_env
):
    campaign = init(study, configs, "fMRIPrep-25.2.5+anat", "MRIQC-24.0.2", limit=1)
    config = yaml.safe_load((campaign / campaign_mod.CONFIG_FILENAME).read_text())
    assert config == {
        "label": "nprep",
        "apps": [
            "bids-app-configs/fMRIPrep-25.2.5+anat.yaml",
            "bids-app-configs/MRIQC-24.0.2.yaml",
        ],
        "cluster": "clusters/dartmouth.yaml",
        "limit": 1,
    }


def test_the_venv_and_the_flock_are_gitignored_from_inside_mechababs_dir(
    study, configs, stub_env
):
    # mechababs' footprint stays under .mechababs/; the study's own .gitignore is
    # upstream's and is not touched. The venv is the campaign's, the flock is the
    # level's (one lock for every campaign here), so each is ignored from its own
    # home.
    campaign = init(study, configs)
    assert (campaign / ".gitignore").read_text().split() == [".venv/"]
    assert campaign_mod.level_gitignore_path(study).read_text().split() == [
        campaign_mod.FLOCK_FILENAME
    ]
    assert not (study / ".gitignore").exists()


def test_env_sh_selects_the_campaign_and_activates_its_venv(study, configs, stub_env):
    campaign = init(study, configs)
    env_sh = (campaign / campaign_mod.ENV_FILENAME).read_text()
    assert "export MECHABABS_CAMPAIGN='nprep'" in env_sh
    assert ".venv/bin/activate" in env_sh
    # no absolute path: env.sh is committed and must work from any clone
    assert str(study) not in env_sh


def test_the_campaign_is_saved_into_the_study(study, configs, stub_env):
    campaign = init(study, configs)
    saved_study, message, paths = stub_env["save"]
    # Two declared paths: the campaign, and the level's .gitignore — shared by every
    # campaign here, so it is committed with the first and a no-op for the rest.
    assert (saved_study, paths) == (
        study,
        [campaign, campaign_mod.level_gitignore_path(study)],
    )
    assert "campaign init nprep" in message


def test_init_holds_the_level_flock_across_the_save(
    study, configs, stub_env, monkeypatch
):
    """Init commits at the study root, where a tick or an add-dataset may be
    mid-save, so it is a writer like the rest and takes the level's lock — around
    the save, not just the writes, since the save is the collision."""
    events = []
    real_flocked = campaign_init.flocked

    @contextmanager
    def watching_flock(lock):
        with real_flocked(lock):
            events.append(("locked", Path(lock)))
            yield
            events.append(("released", Path(lock)))

    stubbed_save = campaign_init.campaign_save_scope

    @contextmanager
    def watching_save(root, paths):
        with stubbed_save(root, paths) as save:
            yield save
        events.append(("saved", root))

    monkeypatch.setattr(campaign_init, "flocked", watching_flock)
    monkeypatch.setattr(campaign_init, "campaign_save_scope", watching_save)

    init(study, configs)

    lock = campaign_mod.flock_path(study)
    assert events == [("locked", lock), ("saved", study), ("released", lock)]


def test_a_later_init_leaves_the_level_gitignore_alone(study, configs, stub_env):
    """The level's .gitignore is shared by every campaign here and may carry a
    user's own lines: the first init writes it, later ones do not touch it."""
    init(study, configs, label="first")
    gitignore = campaign_mod.level_gitignore_path(study)
    gitignore.write_text(f"{campaign_mod.FLOCK_FILENAME}\nmy-scratch/\n")

    campaign = init(study, configs, label="second")

    assert gitignore.read_text() == f"{campaign_mod.FLOCK_FILENAME}\nmy-scratch/\n"
    # still declared: a user's uncommitted edit to it is refused, not swept in
    _, _, paths = stub_env["save"]
    assert paths == [campaign, gitignore]


# --- git routing ------------------------------------------------------------


def test_the_campaign_declares_its_own_git_routing(study, configs, stub_env):
    # an attribute on the campaign dir, so it holds for every writer into it — not
    # a flag one save happens to pass
    campaign = init(study, configs)
    assert (campaign / ".gitattributes").read_text() == campaign_init.GITATTRIBUTES


def test_campaign_files_land_in_git_not_annex(tmp_path, configs, monkeypatch):
    """In a real datalad dataset: every campaign file is in git, none annexed.

    The routing is git-annex's decision at add time, so only a real `datalad save`
    into a real dataset proves the `.gitattributes` does it. Local only — no network.
    """
    if shutil.which("git-annex") is None:
        pytest.skip("git-annex is needed to prove a file was NOT annexed")
    from datalad.api import Dataset

    root = tmp_path / "real-study"
    Dataset(str(root)).create(result_renderer="disabled")
    monkeypatch.setattr(campaign_init, "build_env", pretend_build_env)

    campaign = campaign_init.init(
        root,
        "nprep",
        [str(configs / "MRIQC-24.0.2.yaml")],
        str(configs / "dartmouth.yaml"),
    )

    listing = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-s",
            "--",
            str(campaign.relative_to(root)),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    entries = {line.split("\t", 1)[1]: line.split()[0] for line in listing}
    # 120000 is a symlink — how an annexed file is committed
    annexed = [name for name, mode in entries.items() if mode != "100644"]
    assert not annexed, f"annexed instead of git: {annexed}"
    # the attribute file itself is committed, or a clone routes its own writes wrong
    assert ".mechababs/campaigns/nprep/.gitattributes" in entries
    assert f".mechababs/campaigns/nprep/{campaign_mod.UV_LOCK_FILENAME}" in entries
    # the venv is ignored, not committed
    assert not [name for name in entries if f"/{campaign_mod.VENV_DIRNAME}/" in name]
    assert (
        subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )


# --- the save scope ---------------------------------------------------------


@pytest.fixture
def real_study(tmp_path):
    """A real datalad dataset — the save scope talks to git, so it needs one."""
    if shutil.which("git-annex") is None:
        pytest.skip("git-annex is needed for a real datalad dataset")
    from datalad.api import Dataset

    root = tmp_path / "real-study"
    Dataset(str(root)).create(result_renderer="disabled")
    return root


def test_the_save_scope_commits_only_its_own_target(real_study):
    target = real_study / ".mechababs" / "campaigns" / "nprep"
    (real_study / "upstream-edit.txt").write_text("someone else's work\n")

    with campaign_init.campaign_save_scope(real_study, target) as save:
        target.mkdir(parents=True)
        (target / "campaign.yaml").write_text("label: nprep\n")
        save.message = "campaign init nprep"

    log = subprocess.run(
        ["git", "-C", str(real_study), "log", "-1", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert log == "campaign init nprep"
    # the unrelated edit is still sitting there, untouched and uncommitted
    assert (
        subprocess.run(
            ["git", "-C", str(real_study), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == "?? upstream-edit.txt\n"
    )


def test_the_save_scope_refuses_a_target_that_is_already_dirty(real_study):
    # add-dataset's case: a hand-edit sitting in the campaign dir would otherwise be
    # swept into a commit attributed to mechababs
    target = real_study / ".mechababs" / "campaigns" / "nprep"
    target.mkdir(parents=True)
    (target / "hand-edit.yaml").write_text("someone was here\n")

    with pytest.raises(SystemExit) as e:
        with campaign_init.campaign_save_scope(real_study, target):
            pass  # never reached: the guard is at entry
    assert "hand-edit.yaml" in str(e.value)


def test_the_save_scope_ignores_dirt_outside_its_target(real_study):
    # path-scoped, so a dirty study elsewhere is not this save's problem — and the
    # check never walks the study's sourcedata
    target = real_study / ".mechababs" / "campaigns" / "nprep"
    (real_study / "elsewhere.txt").write_text("dirty\n")

    with campaign_init.campaign_save_scope(real_study, target) as save:
        target.mkdir(parents=True)
        (target / "campaign.yaml").write_text("label: nprep\n")
        save.message = "campaign init nprep"


# --- the pins ---------------------------------------------------------------


def test_babs_defaults_to_the_released_version_from_pypi(study, configs, stub_env):
    # no --babs: a plain dependency with no source entry, so uv resolves the latest
    # release and the lock freezes the exact version. Git is the override, not the default.
    campaign = init(study, configs)
    pyproject = (campaign / campaign_mod.PYPROJECT_FILENAME).read_text()
    assert '    "babs",' in pyproject
    sources = pyproject.partition("[tool.uv.sources]")[2].splitlines()
    assert not [line for line in sources if line.startswith("babs = ")]


def test_datalad_is_pinned_to_the_run_fix_branch(study, configs, stub_env):
    # TEMPORARY — delete with campaign_init.DATALAD_PIN (datalad/datalad#7904):
    # until a datalad release carries the fix, every campaign pins the fix branch,
    # which requires promoting datalad to a direct dependency.
    campaign = init(study, configs)
    pyproject = (campaign / campaign_mod.PYPROJECT_FILENAME).read_text()
    assert '    "datalad",' in pyproject
    pin = campaign_init.DATALAD_PIN
    assert f'datalad = {{ git = "{pin["git"]}", rev = "{pin["rev"]}" }}' in pyproject


def test_pyproject_pins_mechababs_and_babs_by_ref(study, configs, stub_env):
    campaign = init(
        study,
        configs,
        babs_spec="https://github.com/PennLINC/babs.git@v0.5.0",
        mechababs_spec="https://github.com/con/mechababs.git@v0.2",
    )
    pyproject = (campaign / campaign_mod.PYPROJECT_FILENAME).read_text()
    assert (
        'babs = { git = "https://github.com/PennLINC/babs.git", rev = "v0.5.0" }'
        in pyproject
    )
    assert (
        'mechababs = { git = "https://github.com/con/mechababs.git", rev = "v0.2" }'
        in pyproject
    )
    # a virtual project: the campaign is dependencies to resolve, not a package to build
    assert "[build-system]" not in pyproject


def test_a_config_path_with_a_literal_tilde_is_expanded(tmp_path, monkeypatch):
    # `--apps a.yaml,~/b.yaml`: the shell expands only a word-initial `~`, so the
    # second path arrives with the `~` still in it
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "MRIQC-24.0.2.yaml").write_text("{}\n")
    dest = tmp_path / "staged"
    assert campaign_init.stage_config(dest, "~/MRIQC-24.0.2.yaml", "app") == (
        "MRIQC-24.0.2.yaml"
    )
    assert (dest / "MRIQC-24.0.2.yaml").is_file()


def test_a_local_checkout_pin_becomes_a_file_url(tmp_path):
    # dev mode: run a whole campaign against a branch that exists only on disk
    checkout = tmp_path / "babs-checkout"
    checkout.mkdir()
    assert campaign_init.git_source(str(checkout), "my-branch") == {
        "git": checkout.resolve().as_uri(),
        "rev": "my-branch",
    }


def test_a_git_plus_url_is_normalised(tmp_path):
    assert (
        campaign_init.git_source("git+https://x/y.git", "main")["git"]
        == "https://x/y.git"
    )


def test_a_pin_without_a_ref_is_refused():
    with pytest.raises(SystemExit):
        campaign_init.parse_source_spec("https://github.com/PennLINC/babs.git", "babs")


def test_the_running_mechababs_is_pinned_by_its_resolved_commit(monkeypatch):
    class FakeDist:
        version = "0.2"

        def read_text(self, name):
            return (
                '{"url": "https://github.com/con/mechababs",'
                ' "vcs_info": {"vcs": "git", "requested_revision": "v0.2",'
                ' "commit_id": "9f3c1a2"}}'
            )

    monkeypatch.setattr(campaign_init.metadata, "distribution", lambda n: FakeDist())
    # the commit, not the branch: the campaign records exactly what ran
    assert campaign_init.running_mechababs_pin() == (
        "mechababs",
        {"git": "https://github.com/con/mechababs", "rev": "9f3c1a2"},
    )


def test_an_editable_mechababs_is_pinned_by_path(monkeypatch):
    class FakeDist:
        version = "0.2"

        def read_text(self, name):
            return (
                '{"url": "file:///home/dev/mechababs", "dir_info": {"editable": true}}'
            )

    monkeypatch.setattr(campaign_init.metadata, "distribution", lambda n: FakeDist())
    assert campaign_init.running_mechababs_pin() == (
        "mechababs",
        {"path": "/home/dev/mechababs", "editable": True},
    )


def test_a_released_mechababs_is_pinned_by_version(monkeypatch):
    class FakeDist:
        version = "0.2.1"

        def read_text(self, name):
            return None  # a registry install has no direct_url.json

    monkeypatch.setattr(campaign_init.metadata, "distribution", lambda n: FakeDist())
    assert campaign_init.running_mechababs_pin() == ("mechababs==0.2.1", None)


# --- the cluster's env_constraints ------------------------------------------


def parsed_pyproject(campaign):
    """The generated pyproject, through a real TOML parser.

    Asserting on parsed structure rather than rendered text is what proves uv can read
    what we emit — the `[tool.uv]` block sits after `[tool.uv.sources]`, which is a
    super-table-after-sub-table ordering worth checking rather than assuming.
    """
    tomllib = pytest.importorskip("tomllib")  # stdlib from 3.11; mechababs targets 3.10
    return tomllib.loads((campaign / campaign_mod.PYPROJECT_FILENAME).read_text())


def test_the_clusters_env_constraints_reach_the_pyproject(study, configs, stub_env):
    # a site fact (an old glibc's wheels), declared on the cluster axis and folded in
    # verbatim — mechababs does not interpret the specifiers
    campaign = init(study, configs, cluster="old-glibc")
    assert parsed_pyproject(campaign)["tool"]["uv"]["constraint-dependencies"] == [
        "pandas<=2.3.2",
        "h5py<=3.14.0",
    ]


def test_a_cluster_without_env_constraints_declares_none(study, configs, stub_env):
    # absent means no constraints: a modern cluster's pyproject is unchanged
    campaign = init(study, configs)
    pyproject = (campaign / campaign_mod.PYPROJECT_FILENAME).read_text()
    assert "constraint-dependencies" not in pyproject


def test_env_constraints_compose_with_the_source_pins(study, configs, stub_env):
    # both blocks land in one pyproject, and neither eats the other
    campaign = init(
        study,
        configs,
        cluster="old-glibc",
        babs_spec="https://github.com/PennLINC/babs.git@v0.5.0",
    )
    uv = parsed_pyproject(campaign)["tool"]["uv"]
    assert uv["sources"]["babs"]["rev"] == "v0.5.0"
    assert uv["constraint-dependencies"] == ["pandas<=2.3.2", "h5py<=3.14.0"]


def test_build_env_is_told_which_cluster_config_to_blame(study, configs, stub_env):
    # the staged copy, so a failure message points at the file committed in the campaign
    campaign = init(study, configs, cluster="old-glibc")
    _, cluster_file = stub_env["build_env"]
    assert Path(cluster_file) == campaign / "clusters" / "old-glibc.yaml"


@pytest.mark.parametrize(
    "written, why",
    [
        (
            "env_constraints: pandas<=2.3.2\n",
            "a scalar would be iterated one constraint per CHARACTER",
        ),
        (
            "env_constraints:\n  pandas: '<=2.3.2'\n",
            "a mapping would silently yield its KEYS, dropping every specifier",
        ),
        ("env_constraints:\n  - 3\n", "a non-string is not a specifier"),
    ],
)
def test_env_constraints_that_is_not_a_list_of_strings_is_refused(
    tmp_path, written, why
):
    bad = tmp_path / "bad-cluster.yaml"
    bad.write_text(written)
    with pytest.raises(SystemExit) as e:
        campaign_init.cluster_env_constraints(bad)
    assert "must be a LIST" in str(e.value), why


# --- a package with no wheel for this system --------------------------------


@pytest.fixture
def fake_uv(tmp_path, monkeypatch):
    """Stand in for `uv` with a script that prints given output and fails.

    A missing-wheel failure is a real network resolve plus a real compiler error, so it
    cannot be provoked in a unit test — but what mechababs has to get right is only the
    reading of uv's output, which this pins exactly.
    """

    def install(output, returncode=1):
        script = tmp_path / "fake-uv"
        script.write_text(
            f"#!/usr/bin/env bash\ncat <<'UVEOF'\n{output}\nUVEOF\nexit {returncode}\n"
        )
        script.chmod(0o755)
        monkeypatch.setattr(campaign_init, "UV", str(script))
        return script

    return install


# What uv prints when a package has no wheel for this platform: it falls back to the
# sdist, the build backend fails, and this line precedes hundreds of compiler lines.
UV_BUILD_FAILURE = """\
Resolved 128 packages in 1.20s
  × Failed to build `pandas==2.3.3`
  ├─▶ The build backend returned an error
  ╰─▶ Call to `setuptools.build_meta.build_wheel` failed (exit status: 1)
      pandas/_libs/tslibs/base.c:31:10: fatal error: Python.h: No such file
"""


def test_a_missing_wheel_names_the_package_and_the_knob(fake_uv, tmp_path):
    fake_uv(UV_BUILD_FAILURE)
    campaign = tmp_path / "study" / ".mechababs" / "campaigns" / "nprep"
    cluster = campaign / "clusters" / "sherlock.yaml"

    with pytest.raises(SystemExit) as e:
        campaign_init.run_uv(
            "sync", "--frozen", campaign=campaign, cluster_file=cluster
        )

    message = str(e.value)
    # the package, not the header file the compiler complained about
    assert "pandas" in message
    # the lever, and the file to pull it in
    assert "env_constraints" in message
    assert str(cluster) in message
    # the retry path: init does not re-run over an existing campaign
    assert str(campaign) in message


def test_a_failure_that_is_not_a_build_failure_is_not_blamed_on_the_cluster(
    fake_uv, tmp_path
):
    # an unreachable pin, no network, a bad specifier — saying `env_constraints` here
    # would send the user to edit the one file that is fine
    fake_uv("error: Git operation failed\n  ╰─▶ failed to clone into: /tmp/x")
    with pytest.raises(SystemExit) as e:
        campaign_init.run_uv(
            "lock", campaign=tmp_path, cluster_file=tmp_path / "c.yaml"
        )
    assert "env_constraints" not in str(e.value)


def test_a_uv_command_that_succeeds_returns_and_streams(fake_uv, capfd):
    fake_uv("Resolved 128 packages in 1.20s", returncode=0)
    campaign_init.run_uv("lock", campaign="/x", cluster_file="/x/c.yaml")
    # kept AND shown: a resolve is slow, so swallowing its progress would be worse
    assert "Resolved 128 packages" in capfd.readouterr().err


# --- refusals ---------------------------------------------------------------


def test_init_refuses_a_second_campaign_under_the_same_label(study, configs, stub_env):
    init(study, configs)
    with pytest.raises(SystemExit):
        init(study, configs)


def test_init_refuses_a_duplicate_app_name_before_copying_anything(
    study, configs, stub_env, tmp_path
):
    other = tmp_path / "other"
    other.mkdir()
    (other / "MRIQC-24.0.2.yaml").write_text("bids_app_args: {x: 1}\n")
    with pytest.raises(SystemExit):
        campaign_init.init(
            study,
            "nprep",
            [str(configs / "MRIQC-24.0.2.yaml"), str(other / "MRIQC-24.0.2.yaml")],
            str(configs / "dartmouth.yaml"),
        )
    assert not (campaign_mod.apps_dir(study, "nprep") / "MRIQC-24.0.2.yaml").exists()


def test_init_refuses_a_depends_on_outside_the_bundle(study, configs, stub_env):
    # fMRIPrep-minimal depends on fMRIPrep-anat, which is not in this bundle
    with pytest.raises(SystemExit) as e:
        init(study, configs, "MRIQC-24.0.2", "fMRIPrep-25.2.5+minimal")
    assert "depends_on" in str(e.value)


def test_init_accepts_a_declared_dependency_that_is_in_the_bundle(
    study, configs, stub_env
):
    campaign = init(study, configs, "fMRIPrep-25.2.5+anat", "fMRIPrep-25.2.5+minimal")
    assert (
        campaign_init.declared_depends_on(
            campaign / "bids-app-configs" / "fMRIPrep-25.2.5+minimal.yaml"
        )
        == "fMRIPrep-25.2.5+anat"
    )


def test_init_refuses_a_config_that_is_not_there(study, configs, stub_env):
    # a bare name is NOT resolved against some directory mechababs knows about
    with pytest.raises(SystemExit):
        campaign_init.init(
            study, "nprep", ["MRIQC-24.0.2.yaml"], str(configs / "dartmouth.yaml")
        )


def test_init_refuses_an_unusable_label(study, configs, stub_env):
    with pytest.raises(SystemExit):
        init(study, configs, label="../escape")


def test_init_refuses_outside_a_study(tmp_path, configs, stub_env):
    from mechababs import study as study_mod

    with pytest.raises(SystemExit):
        study_mod.require_study_root(tmp_path / "not-a-study")


# --- the real environment build ---------------------------------------------


@pytest.mark.uv_build
def test_uv_really_locks_and_builds_the_campaign_venv(study, configs, monkeypatch):
    """The env build for real: uv resolves the lock and syncs a venv from it.

    Marked so the fast suite skips it — it runs `uv` and reaches the network. The
    mechababs pin is this checkout, so no mechababs release is needed; babs comes
    from PyPI, its default.
    """
    if (
        subprocess.run(["uv", "--version"], capture_output=True, check=False).returncode
        != 0
    ):
        pytest.skip("uv not available")
    saved = {}

    @contextmanager
    def fake_save_scope(root, path):
        pending = utils.PendingSave()
        yield pending
        saved["path"] = path

    monkeypatch.setattr(campaign_init, "campaign_save_scope", fake_save_scope)

    checkout = Path(__file__).resolve().parent.parent
    branch = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    campaign = campaign_init.init(
        study,
        "nprep",
        [str(configs / "MRIQC-24.0.2.yaml")],
        str(configs / "dartmouth.yaml"),
        mechababs_spec=f"{checkout}@{branch}",
    )

    lock = (campaign / campaign_mod.UV_LOCK_FILENAME).read_text()
    assert 'name = "babs"' in lock and 'name = "mechababs"' in lock
    # the venv is where env.sh will look ...
    venv = campaign / campaign_mod.VENV_DIRNAME
    assert (venv / "bin" / "mechababs").exists()
    # ... and it carries the uv that checks it, resolved from the venv rather than
    # PATH, so the guard is self-contained wherever the campaign is later cloned
    assert (venv / "bin" / "uv").exists()
    # ... so both halves of the env-match guard pass against a freshly built campaign,
    # with no stamp and nothing else recorded about the environment
    monkeypatch.setattr("sys.prefix", str(venv))
    campaign_mod.require_env_match(study, "nprep")
    # and the study-local check the inner verbs carry passes on the same venv
    campaign_mod.require_study_lock_match(study, "nprep")


# --- the superstudy level ---------------------------------------------------
#
# The level changes exactly one thing about a campaign: which bookkeeping file it
# carries. Everything else — configs, pins, lock, env.sh — is identical, so these
# tests pin the asymmetry and its absence rather than re-checking the layout.


def test_a_superstudy_campaign_carries_membership_and_no_statefile(
    study, configs, stub_env
):
    campaign = init(study, configs, superstudy=True)

    assert (campaign / campaign_mod.MEMBERS_FILENAME).is_file()
    assert not (campaign / campaign_mod.STATE_FILENAME).exists()
    assert campaign_mod.is_superstudy_campaign(study, "nprep")


def test_a_study_campaign_carries_a_statefile_and_no_membership(
    study, configs, stub_env
):
    campaign = init(study, configs)

    assert (campaign / campaign_mod.STATE_FILENAME).is_file()
    assert not (campaign / campaign_mod.MEMBERS_FILENAME).exists()
    assert not campaign_mod.is_superstudy_campaign(study, "nprep")


def test_the_membership_catalog_is_header_only(study, configs, stub_env):
    # no members are selected at init: a member's footprint is written when
    # add-dataset first selects into it, so there is nothing to fan out to yet
    campaign = init(study, configs, superstudy=True)
    assert (
        campaign / campaign_mod.MEMBERS_FILENAME
    ).read_text() == campaign_mod.initial_members_header()


def test_the_level_does_not_change_anything_else(study, configs, stub_env, tmp_path):
    other = tmp_path / "study-ds000002"
    (other / ".datalad").mkdir(parents=True)

    at_study = init(study, configs, "MRIQC-24.0.2")
    at_super = init(other, configs, "MRIQC-24.0.2", superstudy=True)

    same = (
        campaign_mod.CONFIG_FILENAME,
        campaign_mod.PYPROJECT_FILENAME,
        campaign_mod.ENV_FILENAME,
    )
    for name in same:
        assert (at_study / name).read_text() == (at_super / name).read_text(), name


# --- creating or adopting the superstudy dataset ----------------------------


def test_an_absent_superstudy_is_created_as_a_dataset(tmp_path):
    path = campaign_init.create_superstudy(tmp_path / "my-super")

    assert path == (tmp_path / "my-super").resolve()
    assert (path / ".datalad").is_dir()


def test_an_existing_superstudy_is_adopted_untouched(tmp_path):
    # a superstudy that has run campaigns before gains another by the same
    # command that creates one from scratch — adoption is not a separate mode
    existing = tmp_path / "my-super"
    (existing / ".datalad").mkdir(parents=True)
    (existing / ".mechababs" / "campaigns" / "last-year").mkdir(parents=True)

    path = campaign_init.create_superstudy(existing)

    assert path == existing.resolve()
    assert (existing / ".mechababs" / "campaigns" / "last-year").is_dir()


def test_a_populated_non_dataset_directory_is_refused(tmp_path):
    # converting a directory that already holds files is a different act from
    # creating a coordinator, and not one init should perform silently
    occupied = tmp_path / "my-super"
    occupied.mkdir()
    (occupied / "notes.txt").write_text("mine\n")

    with pytest.raises(SystemExit) as excinfo:
        campaign_init.create_superstudy(occupied)
    assert "not a datalad dataset" in str(excinfo.value)


# --- the CLI: naming the target ---------------------------------------------


def test_cli_refuses_superstudy_and_dataset_together(monkeypatch, capsys):
    """Both name the target, so accepting both would leave which one wins to a
    reading of the source rather than the help text."""
    import sys as _sys

    from mechababs import cli

    monkeypatch.setattr(
        _sys,
        "argv",
        [
            "mechababs",
            "campaign",
            "init",
            "nprep",
            "-d",
            "/some/study",
            "--superstudy",
            "my-super",
            "--apps",
            "a.yaml",
            "--cluster",
            "c.yaml",
        ],
    )
    with pytest.raises(SystemExit):
        cli.main()
    assert "not allowed with argument" in capsys.readouterr().err


def test_cli_defaults_the_dataset_to_the_current_directory(monkeypatch, tmp_path):
    """`-d` mirrors datalad's, and its absence is the common case: you are
    standing in the study."""
    import sys as _sys

    from mechababs import cli

    study = tmp_path / "study-ds000001"
    (study / ".datalad").mkdir(parents=True)
    monkeypatch.chdir(study)

    seen = {}
    monkeypatch.setattr(
        cli.campaign_init,
        "init",
        lambda root, *a, **kw: seen.update(root=root, kw=kw) or (root / "c"),
    )
    monkeypatch.setattr(
        _sys,
        "argv",
        [
            "mechababs",
            "campaign",
            "init",
            "nprep",
            "--apps",
            "a.yaml",
            "--cluster",
            "c.yaml",
        ],
    )
    cli.main()

    assert seen["root"] == study.resolve()
    assert seen["kw"]["superstudy"] is False


def test_the_retry_advice_is_the_calling_verbs(fake_uv, tmp_path):
    """Same diagnosis, different way back. `init` cannot re-run over an existing
    campaign, so it says to remove the half-built one; `update-env` converges an
    existing campaign, and telling its user to `rm -rf` would delete a campaign
    holding real derivatives and history.
    """
    fake_uv(UV_BUILD_FAILURE)
    campaign = tmp_path / "study" / ".mechababs" / "campaigns" / "nprep"
    uv = dict(campaign=campaign, cluster_file=campaign / "clusters" / "sherlock.yaml")

    with pytest.raises(SystemExit) as init_failure:
        campaign_init.run_uv("sync", **uv)
    with pytest.raises(SystemExit) as update_failure:
        campaign_init.run_uv("sync", **uv, retry=campaign_init.UPDATE_ENV_RETRY)

    assert f"rm -rf {campaign}" in str(init_failure.value)
    assert "campaign init" in str(init_failure.value)

    assert "rm -rf" not in str(update_failure.value)
    assert "campaign update-env" in str(update_failure.value)
    # the diagnosis half is shared, not duplicated
    for message in (str(init_failure.value), str(update_failure.value)):
        assert "pandas" in message and "env_constraints" in message
