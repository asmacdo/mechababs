"""Tests for `mechababs test-cluster` — resolution and the pytest invocation.

The scenario itself is only exercised on a real cluster (that is the point of it), so
what is unit-testable is the wiring: which config gets picked, where the scenario is
built, and that the command runs the campaign venv's own pytest over the packaged
suite rather than anything ambient.
"""

import sys

import pytest
from mechababs import validate


@pytest.fixture
def campaign(tmp_path):
    """A campaign skeleton with one cluster config in it."""
    path = tmp_path / "my-campaign"
    (path / "clusters").mkdir(parents=True)
    (path / "clusters" / "sherlock.yaml").write_text("cluster_resources: {}\n")
    return path


def test_resolve_cluster_finds_a_bare_name_in_the_campaign(campaign):
    """The campaign owns its configs, so a name resolves there."""
    resolved = validate.resolve_cluster(campaign, "sherlock.yaml")
    assert resolved == (campaign / "clusters" / "sherlock.yaml").resolve()


def test_resolve_cluster_takes_a_path_as_given(campaign, tmp_path):
    """Validating a config you have not adopted yet is the main use case."""
    outside = tmp_path / "new-site.yaml"
    outside.write_text("cluster_resources: {}\n")
    assert validate.resolve_cluster(campaign, str(outside)) == outside.resolve()


def test_resolve_cluster_does_not_search_a_vendored_examples_dir(campaign):
    """Guard against reintroducing the `code/mechababs/examples/` lookup: that path
    disappears once code is referenced and locked rather than cloned in."""
    examples = campaign / "code" / "mechababs" / "examples" / "clusters"
    examples.mkdir(parents=True)
    (examples / "vendored.yaml").write_text("cluster_resources: {}\n")
    with pytest.raises(SystemExit, match="cluster config not found"):
        validate.resolve_cluster(campaign, "vendored.yaml")


def test_resolve_cluster_names_where_it_looked(campaign):
    with pytest.raises(SystemExit, match=str(campaign / "clusters")):
        validate.resolve_cluster(campaign, "absent.yaml")


def test_default_workdir_is_beside_the_campaign(campaign):
    """The pipelines resolve their shim as `../repronim-containers-shim`, so the
    scenario's campaign must share a parent with the campaign under test."""
    assert validate.default_workdir(campaign) == campaign.parent


def test_pytest_command_runs_this_interpreter_over_the_packaged_suite(campaign):
    from mechababs import testing

    cmd = validate.pytest_command(campaign / "clusters" / "sherlock.yaml", campaign)
    assert cmd[:3] == [sys.executable, "-m", "pytest"], "must not use an ambient pytest"
    assert str(testing.suite_path()) in cmd
    assert "--campaign" in cmd and str(campaign) in cmd
    assert "--cluster-config" in cmd


def test_pytest_command_passes_extra_args_through(campaign):
    cmd = validate.pytest_command(
        campaign / "clusters" / "sherlock.yaml",
        campaign,
        ["-k", "test_full_run"],
    )
    assert cmd[-2:] == ["-k", "test_full_run"]


def _parse_cli(argv):
    """What the real CLI makes of an argv, stopping before it runs anything.

    Drives `cli.main()` rather than calling the module functions directly: passing an
    already-split list to `pytest_command` cannot catch an argparse-level problem, and
    a flag-passthrough IS an argparse-level problem.
    """
    from mechababs import cli

    seen = {}

    def spy(campaign, cluster, extra_args=(), workdir=None):
        seen["cluster"], seen["extra"] = cluster, list(extra_args)
        return 0

    original_run = validate.run_test_cluster
    original_checks = (
        cli._ensure_campaign_skeleton,
        cli.guard.require_clean_pins,
        cli._require_campaign_venv,
    )
    validate.run_test_cluster = spy
    cli._ensure_campaign_skeleton = lambda args: args.campaign_path
    cli.guard.require_clean_pins = lambda campaign: None
    cli._require_campaign_venv = lambda campaign: None
    try:
        import sys as _sys

        argv_backup = _sys.argv
        _sys.argv = ["mechababs", *argv]
        try:
            cli.main()
        finally:
            _sys.argv = argv_backup
    finally:
        validate.run_test_cluster = original_run
        (
            cli._ensure_campaign_skeleton,
            cli.guard.require_clean_pins,
            cli._require_campaign_venv,
        ) = original_checks
    return seen


def test_cli_forwards_pytest_flags_after_a_double_dash():
    """The documented passthrough must actually parse. `argparse.REMAINDER` only
    reaches flag-looking tokens once `--` fences them off, and it keeps the `--`, so
    both halves of that (parsing, and stripping) are asserted here."""
    seen = _parse_cli(
        ["test-cluster", "--cluster", "x.yaml", "--", "-k", "test_full_run"]
    )
    assert seen["extra"] == ["-k", "test_full_run"], "the `--` must not reach pytest"


def test_cli_accepts_no_pytest_args():
    seen = _parse_cli(["test-cluster", "--cluster", "x.yaml"])
    assert seen["extra"] == []


def test_test_cluster_runs_on_an_unconfigured_campaign(tmp_path, monkeypatch):
    """`test-cluster` must not demand the ledger, which only `configure` writes.

    The documented order is bootstrap -> validate -> configure: you validate a cluster
    *before* committing real data to it. Gating on the ledger made that order impossible,
    so every dev run and every first user run died at "not a campaign". Drives the real
    CLI, stubbing only the checks that need a live campaign.
    """
    from mechababs import cli

    campaign = tmp_path / "fresh-campaign"
    (campaign / ".datalad").mkdir(parents=True)
    for pin in ("code/mechababs", "code/babs"):
        (campaign / pin).mkdir(parents=True)
    (campaign / "clusters").mkdir()
    (campaign / "clusters" / "site.yaml").write_text("cluster_resources: {}\n")
    # Deliberately no desc-mechababs_datasets.tsv: this is what bootstrap.sh leaves.

    seen = {}

    def spy(camp, cluster, extra_args=(), workdir=None):
        seen["campaign"] = camp
        return 0

    monkeypatch.setattr(cli.guard, "require_clean_pins", lambda c: None)
    monkeypatch.setattr(cli, "_require_campaign_venv", lambda c: None)
    monkeypatch.setattr(validate, "run_test_cluster", spy)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mechababs",
            "test-cluster",
            "--campaign-path",
            str(campaign),
            "--cluster",
            "site.yaml",
        ],
    )

    assert cli.main() == 0
    assert seen["campaign"] == campaign.resolve()


# --- the campaign skeleton: what `configure` and `test-cluster` both require -------
# Extracted so those two commands share one notion of "campaign enough to run", which
# only holds if the checks themselves stay. Tested directly, because the CLI tests
# above stub the function out.


def _args(campaign):
    from types import SimpleNamespace

    return SimpleNamespace(campaign_path=campaign)


def test_campaign_skeleton_refuses_a_plain_directory(tmp_path):
    """No `.datalad` means bootstrap.sh never ran here — there are no pins to
    provision from and no dataset to record anything in."""
    from mechababs import cli

    with pytest.raises(SystemExit) as exc:
        cli._ensure_campaign_skeleton(_args(tmp_path / "not-a-campaign"))
    assert "not a datalad dataset" in str(exc.value)


@pytest.mark.parametrize("present", ["code/mechababs", "code/babs"])
def test_campaign_skeleton_refuses_a_missing_code_pin(tmp_path, present):
    """Either pin missing is fatal: the pins ARE the provisioning input, so a
    half-built campaign would validate the wrong code or none at all."""
    from mechababs import cli

    campaign = tmp_path / "half-built"
    (campaign / ".datalad").mkdir(parents=True)
    (campaign / present).mkdir(parents=True)

    with pytest.raises(SystemExit) as exc:
        cli._ensure_campaign_skeleton(_args(campaign))
    assert "not a campaign skeleton" in str(exc.value)


def test_campaign_skeleton_accepts_what_bootstrap_leaves(tmp_path):
    """The positive case: a datalad dataset with both pins and no ledger."""
    from mechababs import cli

    campaign = tmp_path / "fresh"
    (campaign / ".datalad").mkdir(parents=True)
    for pin in ("code/mechababs", "code/babs"):
        (campaign / pin).mkdir(parents=True)

    assert cli._ensure_campaign_skeleton(_args(campaign)) == campaign.resolve()


# --- clone_ref: what bootstrap can actually re-clone -------------------------------
# This is the riskiest logic in the feature (a pin that resolves to something
# unclonable fails deep inside bootstrap.sh), and it is shared by both provisioning
# routes, so it gets real git repos rather than mocks.


def _repo(path, tag=None):
    import subprocess

    path.mkdir(parents=True)
    run = lambda *a: subprocess.run(
        ["git", "-C", str(path), *a], check=True, capture_output=True
    )
    run("init", "-q", "-b", "main")
    run(
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "c",
    )
    if tag:
        run("tag", tag)
    return path


def test_clone_ref_returns_the_branch(tmp_path):
    assert validate.clone_ref(_repo(tmp_path / "b")) == "main"


def test_clone_ref_falls_back_to_the_tag_when_detached(tmp_path):
    """A tag pin leaves a detached HEAD, where `--abbrev-ref` says the literal
    "HEAD" — which bootstrap's `git clone --branch` cannot resolve."""
    import subprocess

    src = _repo(tmp_path / "src", tag="v1.2.3")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--branch", "v1.2.3", str(src), str(clone)],
        check=True,
        capture_output=True,
    )
    assert validate.clone_ref(clone) == "v1.2.3", "must recover something re-clonable"


def test_clone_ref_refuses_a_detached_commit_with_no_tag(tmp_path):
    import subprocess

    repo = _repo(tmp_path / "d")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", sha], check=True, capture_output=True
    )
    with pytest.raises(validate.RefError, match="detached HEAD"):
        validate.clone_ref(repo)


def test_require_shim_refuses_when_the_shim_is_missing(tmp_path):
    """Without this precheck the scenario's fixtures skip, pytest exits 0, and
    `test-cluster` reports success having validated nothing."""
    with pytest.raises(SystemExit, match="no container shim"):
        validate.require_shim(tmp_path)


def test_require_shim_accepts_a_built_shim(tmp_path):
    (tmp_path / validate.SHIM_DIRNAME / ".datalad").mkdir(parents=True)
    validate.require_shim(tmp_path)  # must not raise


def test_pytest_command_keeps_caches_out_of_the_campaign():
    """pytest would drop .pytest_cache/ into the campaign's code pin, which
    `guard.require_clean_pins` then reads as dirty — bricking every later command."""
    from pathlib import Path

    cmd = validate.pytest_command(Path("/c/clusters/x.yaml"), Path("/c"))
    assert "no:cacheprovider" in cmd
