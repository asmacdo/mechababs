"""What this design assumes about uv, pinned as tests against a real uv.

The env guard, the inner verbs' check, and `campaign update-env` are all thin
wrappers over uv behaviors. mechababs records nothing about an environment, so if
one of these behaviors changed in a uv release, the change would not show up as a
mechababs test failure anywhere else — it would show up as a campaign that quietly
stopped noticing drift, or a bare `update-env` that chased a branch it should have
left alone. Hence one file that asserts the behaviors themselves.

Marked `uv_build` and deselected by default: these run a real uv, build real
packages, and reach the network for a build backend. The fixtures are tiny local
git repos rather than anything remote, so a PyPI outage or a moved upstream cannot
change what they say.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mechababs import campaign as campaign_mod

pytestmark = pytest.mark.uv_build

UV = "uv"


@pytest.fixture(autouse=True)
def needs_uv():
    if subprocess.run([UV, "--version"], capture_output=True).returncode != 0:
        pytest.skip("uv not available")


def run(*cmd, cwd=None, env=None, check=True):
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"{' '.join(str(c) for c in cmd)} failed ({proc.returncode})\n"
            f"{proc.stdout}\n{proc.stderr}"
        )
    return proc


def git(*args, cwd):
    return run("git", "-c", "user.email=t@t", "-c", "user.name=t", *args, cwd=cwd)


PACKAGE_PYPROJECT = """\
[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"

[project]
name = "{name}"
version = "{version}"
requires-python = ">=3.10"

[tool.setuptools]
py-modules = ["{name}"]
"""


def git_package(root, name="tinydep", version="0.1.0"):
    """A tiny installable package in its own git repo, so a `rev` pin is a real one.

    Local, so nothing here depends on a host being up or a branch elsewhere staying
    put — the point is to *move* the branch on purpose and watch what uv does.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        PACKAGE_PYPROJECT.format(name=name, version=version)
    )
    (root / f"{name}.py").write_text(f'__version__ = "{version}"\n')
    git("init", "-b", "main", "-q", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-qm", f"{name} {version}", cwd=root)
    return head(root)


def bump_package(root, name="tinydep", version="0.2.0"):
    """Move the branch on: a new commit at the tip of `main`."""
    (root / "pyproject.toml").write_text(
        PACKAGE_PYPROJECT.format(name=name, version=version)
    )
    (root / f"{name}.py").write_text(f'__version__ = "{version}"\n')
    git("add", "-A", cwd=root)
    git("commit", "-qm", f"{name} {version}", cwd=root)
    return head(root)


def head(root):
    return git("rev-parse", "HEAD", cwd=root).stdout.strip()


PROJECT_PYPROJECT = """\
[project]
name = "fixture-campaign"
version = "0"
requires-python = ">=3.10"
dependencies = [{deps}]

[tool.uv.sources]
tinydep = {{ git = "{url}", rev = "{rev}" }}
"""


def uv_project(project, package, *, rev="main", extra_deps=()):
    """A campaign-shaped uv project: a virtual project pinning the git package.

    The same shape `render_pyproject` writes — no build-system, dependencies plus
    `[tool.uv.sources]` git entries — so what these tests learn about uv is what a
    real campaign gets.
    """
    project.mkdir(parents=True, exist_ok=True)
    deps = ", ".join(f'"{d}"' for d in ("tinydep", *extra_deps))
    (project / "pyproject.toml").write_text(
        PROJECT_PYPROJECT.format(
            deps=deps, url=Path(package).resolve().as_uri(), rev=rev
        )
    )
    return project


def lock(project, *args):
    return run(UV, "lock", "--project", project, *args)


def sync(project, env_path=None):
    env = {"UV_PROJECT_ENVIRONMENT": str(env_path)} if env_path else None
    return run(UV, "sync", "--project", project, "--frozen", env=env)


def check(project, env_path):
    """The guard's exact invocation, as `venv_matches_lock` builds it."""
    return run(
        UV,
        "sync",
        "--check",
        "--frozen",
        "--offline",
        "--project",
        project,
        env={"UV_PROJECT_ENVIRONMENT": str(env_path)},
        check=False,
    )


# --- `uv sync --check` is the freshness check -------------------------------


def test_check_passes_for_a_venv_synced_from_the_lock(tmp_path):
    git_package(tmp_path / "pkg")
    project = uv_project(tmp_path / "campaign", tmp_path / "pkg")
    lock(project)
    venv = tmp_path / "venv"
    sync(project, venv)

    assert check(project, venv).returncode == 0


def test_check_catches_a_package_that_should_not_be_installed(tmp_path):
    """The drift the stamp was blind to: someone pip-installs into the venv. The
    stamp vouched for the venv on the strength of a file it wrote at build time, so
    anything done to the environment afterwards was invisible to it."""
    git_package(tmp_path / "pkg")
    git_package(tmp_path / "stray", name="tinystray")
    project = uv_project(tmp_path / "campaign", tmp_path / "pkg")
    lock(project)
    venv = tmp_path / "venv"
    sync(project, venv)

    run(UV, "pip", "install", "--python", venv / "bin" / "python", tmp_path / "stray")

    result = check(project, venv)
    assert result.returncode != 0
    assert "tinystray" in (result.stderr + result.stdout)


def test_check_catches_a_package_that_is_missing(tmp_path):
    git_package(tmp_path / "pkg")
    project = uv_project(tmp_path / "campaign", tmp_path / "pkg")
    lock(project)
    venv = tmp_path / "venv"
    sync(project, venv)

    run(UV, "pip", "uninstall", "--python", venv / "bin" / "python", "tinydep")

    result = check(project, venv)
    assert result.returncode != 0
    assert "tinydep" in (result.stderr + result.stdout)


def test_check_needs_no_network(tmp_path):
    """`--offline` is what makes the guard cost the same on a compute node, a login
    node, and a laptop on a train. If a uv release ever needed the index to answer
    this, the guard would start failing exactly where it is least debuggable."""
    git_package(tmp_path / "pkg")
    project = uv_project(tmp_path / "campaign", tmp_path / "pkg")
    lock(project)
    venv = tmp_path / "venv"
    sync(project, venv)

    result = check(project, venv)  # already --offline
    assert result.returncode == 0, result.stderr


def test_uv_project_environment_names_which_venv_is_checked(tmp_path):
    """Without it uv checks the project's own `.venv` and only warns about the
    active one — which at a member is not the venv doing the work. So the guard
    names the environment rather than relying on what happens to be activated.
    """
    git_package(tmp_path / "pkg")
    project = uv_project(tmp_path / "campaign", tmp_path / "pkg")
    lock(project)

    good = tmp_path / "good"
    sync(project, good)
    # the project's DEFAULT environment, deliberately empty/wrong
    run(UV, "venv", project / ".venv")

    assert check(project, good).returncode == 0
    assert check(project, project / ".venv").returncode != 0


def test_uv_project_environment_beats_an_activated_virtual_env(tmp_path):
    """Why the guard names the environment instead of taking uv's `--active`.

    A process knows its own `sys.prefix` for certain; what a shell happens to have
    exported is a different, weaker claim. If VIRTUAL_ENV could redirect the check,
    a stale activation would decide which environment a run is validated against —
    the guard would vouch for a venv that is not the one about to do the work.
    """
    git_package(tmp_path / "pkg")
    project = uv_project(tmp_path / "campaign", tmp_path / "pkg")
    lock(project)

    good = tmp_path / "good"
    sync(project, good)
    stale = tmp_path / "stale"
    run(UV, "venv", stale)

    result = run(
        UV,
        "sync",
        "--check",
        "--frozen",
        "--offline",
        "--project",
        project,
        env={"UV_PROJECT_ENVIRONMENT": str(good), "VIRTUAL_ENV": str(stale)},
        check=False,
    )
    assert result.returncode == 0, (
        f"VIRTUAL_ENV redirected the check away from the named environment\n"
        f"{result.stdout}\n{result.stderr}"
    )


# --- what `uv lock` does, and does not, chase -------------------------------


def test_a_bare_relock_does_not_chase_a_moved_branch_pin(tmp_path):
    """The property bare `update-env` rests on. A pin that says `rev = "main"` is
    resolved ONCE, to a sha; re-locking keeps that sha even after main moves. That
    is what makes bare update-env safe as rebuild-from-lock in every recovery story
    — a fresh clone, a wiped site, a historical checkout — instead of silently
    dragging the campaign onto newer tools.
    """
    package = tmp_path / "pkg"
    first = git_package(package)
    project = uv_project(tmp_path / "campaign", package)
    lock(project)
    assert first in (project / "uv.lock").read_text()

    second = bump_package(package)
    assert second != first

    lock(project)  # bare
    text = (project / "uv.lock").read_text()
    assert first in text, "a bare re-lock chased the branch"
    assert second not in text


def test_upgrade_package_does_chase_the_branch(tmp_path):
    """And the property `--upgrade` rests on: the one case with nothing to hand-edit,
    a pin tracking a branch whose tip moved."""
    package = tmp_path / "pkg"
    first = git_package(package)
    project = uv_project(tmp_path / "campaign", package)
    lock(project)

    second = bump_package(package)
    lock(project, "--upgrade-package", "tinydep")

    text = (project / "uv.lock").read_text()
    assert second in text
    assert first not in text


def test_upgrade_package_touches_only_the_lock(tmp_path):
    """The declaration keeps declaring the branch. Were `--upgrade` to rewrite the
    pyproject to a sha it would collapse intent into resolution, and the campaign
    would lose the record of which branch it follows."""
    package = tmp_path / "pkg"
    git_package(package)
    project = uv_project(tmp_path / "campaign", package)
    lock(project)
    declaration = (project / "pyproject.toml").read_text()

    bump_package(package)
    lock(project, "--upgrade-package", "tinydep")

    assert (project / "pyproject.toml").read_text() == declaration


def test_editing_the_rev_re_resolves_on_a_bare_relock(tmp_path):
    """Switching branch/tag/sha is a one-word hand-edit plus a bare update-env —
    which is why there are no --babs/--mechababs rewrite flags."""
    package = tmp_path / "pkg"
    first = git_package(package)
    project = uv_project(tmp_path / "campaign", package)
    lock(project)

    second = bump_package(package)
    uv_project(project, package, rev=second)  # the hand-edit
    lock(project)

    text = (project / "uv.lock").read_text()
    assert second in text and first not in text


# --- the marquee: a detached study validates its own environment ------------


def test_a_detached_study_can_rebuild_and_pass_its_own_lock_check(
    tmp_path, monkeypatch
):
    """Detached reproduction, end to end and for real.

    A member footprint is a complete uv project — `pyproject.toml` AND `uv.lock`,
    copied down — so a study cloned away from its superstudy holds everything needed
    to rebuild the environment its history names. Someone does exactly that, then
    `datalad rerun`s a recorded inner command, and the verb's check has to pass on a
    venv mechababs never built, at a path it has never heard of — while drift is
    still refused, the half that keeps the reproduction honest rather than merely
    permissive.
    """
    git_package(tmp_path / "pkg")

    # A member's campaign footprint, as `write_member_footprint` leaves it: config,
    # pyproject, lock -- and no venv, because a member is never operated from.
    study = tmp_path / "study-ds000001"
    campaign = campaign_mod.campaign_dir(study, "nprep")
    uv_project(campaign, tmp_path / "pkg", extra_deps=("uv",))
    campaign_mod.config_path(study, "nprep").write_text(
        "label: nprep\nsuperstudy: 99999999-8888-7777-6666-555555555555\n"
    )
    lock(campaign)

    # The re-runner builds a venv from that lock, wherever they like.
    theirs = tmp_path / "their-own-venv"
    sync(campaign, theirs)
    # uv is IN the lock (CAMPAIGN_EXTRAS), so the venv carries the uv that checks it
    assert (theirs / "bin" / "uv").is_file()

    monkeypatch.setattr(sys, "prefix", str(theirs))
    assert campaign_mod.require_study_lock_match(study, "nprep") == campaign

    # ... and an environment that contradicts the lock is still refused.
    run(UV, "pip", "uninstall", "--python", theirs / "bin" / "python", "tinydep")
    with pytest.raises(SystemExit) as e:
        campaign_mod.require_study_lock_match(study, "nprep")
    assert "tinydep" in str(e.value)
