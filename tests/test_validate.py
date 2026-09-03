"""Tests for `mechababs test-cluster` — resolution and the pytest invocation.

The scenario itself is only exercised on a real cluster (that is the point of it), so
what is unit-testable is the wiring: which config gets picked, which options reach the
scenario, and that the command runs this interpreter's own pytest over the packaged
suite rather than anything ambient.
"""

import sys
from pathlib import Path

import pytest
from mechababs import validate


def test_resolve_cluster_takes_a_path_as_given(tmp_path):
    """Validating a config you have not adopted yet is the ONLY use case: a config a
    campaign already holds was validated before it got there."""
    site = tmp_path / "new-site.yaml"
    site.write_text("cluster_resources: {}\n")
    assert validate.resolve_cluster(str(site)) == site.resolve()


def test_resolve_cluster_refuses_a_bare_name(tmp_path, monkeypatch):
    """Path-or-URL, never a name resolved against a directory mechababs knows about —
    the same rule `campaign_init.stage_config` applies to every user-provided config.
    A name sitting in the cwd must not quietly resolve either."""
    (tmp_path / "clusters").mkdir()
    (tmp_path / "clusters" / "sherlock.yaml").write_text("cluster_resources: {}\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="cluster config not found"):
        validate.resolve_cluster("sherlock.yaml")


def test_resolve_cluster_says_how_configs_are_named(tmp_path):
    with pytest.raises(SystemExit, match="given by path"):
        validate.resolve_cluster(str(tmp_path / "absent.yaml"))


def test_pytest_command_runs_this_interpreter_over_the_packaged_suite():
    """`sys.executable -m pytest`, not a bare `pytest`: the run must not drift to an
    ambient interpreter, which is also why the caller needs the `test` extra."""
    from mechababs import testing

    cmd = validate.pytest_command(Path("/site/sherlock.yaml"))
    assert cmd[:3] == [sys.executable, "-m", "pytest"], "must not use an ambient pytest"
    assert str(testing.suite_path()) in cmd
    assert "--cluster-config" in cmd and "/site/sherlock.yaml" in cmd


def test_pytest_command_omits_the_pins_it_was_not_given():
    """An absent `--mechababs` is what makes the fixture campaign self-pin to the
    mechababs running this command, so it must not be passed as an empty value."""
    cmd = validate.pytest_command(Path("/site/sherlock.yaml"))
    assert "--mechababs" not in cmd
    assert "--babs" not in cmd


def test_pytest_command_passes_the_pins_it_was_given():
    cmd = validate.pytest_command(
        Path("/site/sherlock.yaml"),
        mechababs="https://example.invalid/mechababs.git@wip",
        babs="https://example.invalid/babs.git@fix",
    )
    assert cmd[cmd.index("--mechababs") + 1] == (
        "https://example.invalid/mechababs.git@wip"
    )
    assert cmd[cmd.index("--babs") + 1] == "https://example.invalid/babs.git@fix"


def test_pytest_command_passes_extra_args_through():
    cmd = validate.pytest_command(Path("/site/sherlock.yaml"), ["-k", "test_spine"])
    assert cmd[-2:] == ["-k", "test_spine"]


def test_pytest_command_keeps_caches_out_of_the_install_tree():
    """The suite ships inside the package, so for an editable install pytest would
    drop `.pytest_cache/` into a checkout."""
    cmd = validate.pytest_command(Path("/site/sherlock.yaml"))
    assert "no:cacheprovider" in cmd


# --- the CLI wiring ----------------------------------------------------------------
# Driven through `cli.main()` rather than by calling the module functions: passing an
# already-split list to `pytest_command` cannot catch an argparse-level problem, and
# both the `--` passthrough and a stale option name ARE argparse-level problems.


def _parse_cli(argv, monkeypatch):
    """What the real CLI makes of an argv, stopping before it runs anything."""
    from mechababs import cli

    seen = {}

    def spy(cluster, scratch_path, extra_args=(), mechababs=None, babs=None):
        seen.update(
            cluster=cluster,
            scratch_path=scratch_path,
            extra=list(extra_args),
            mechababs=mechababs,
            babs=babs,
        )
        return 0

    monkeypatch.setattr(validate, "run_test_cluster", spy)
    monkeypatch.setattr(sys, "argv", ["mechababs", *argv])
    assert cli.main() == 0
    return seen


def test_cli_forwards_pytest_flags_after_a_double_dash(monkeypatch):
    """The documented passthrough must actually parse. `argparse.REMAINDER` only
    reaches flag-looking tokens once `--` fences them off, and it keeps the `--`, so
    both halves of that (parsing, and stripping) are asserted here."""
    seen = _parse_cli(
        [
            "test-cluster",
            "--cluster",
            "x.yaml",
            "--scratch-path",
            "/scratch/me",
            "--",
            "-k",
            "test_spine",
        ],
        monkeypatch,
    )
    assert seen["extra"] == ["-k", "test_spine"], "the `--` must not reach pytest"


def test_cli_accepts_no_pytest_args(monkeypatch):
    seen = _parse_cli(
        ["test-cluster", "--cluster", "x.yaml", "--scratch-path", "/scratch/me"],
        monkeypatch,
    )
    assert seen["extra"] == []
    assert seen["scratch_path"] == "/scratch/me"


def test_cli_leaves_both_pins_unset_by_default(monkeypatch):
    """Unset is the self-pin mode, and the reason `test-cluster` needs no campaign to
    stand in: the mechababs running the command is the one validated."""
    seen = _parse_cli(
        ["test-cluster", "--cluster", "x.yaml", "--scratch-path", "/scratch/me"],
        monkeypatch,
    )
    assert seen["mechababs"] is None
    assert seen["babs"] is None


def test_cli_forwards_both_pins(monkeypatch):
    """Overriding either pin is how a branch gets tested."""
    seen = _parse_cli(
        [
            "test-cluster",
            "--cluster",
            "x.yaml",
            "--scratch-path",
            "/scratch/me",
            "--mechababs",
            "/checkout@wip",
            "--babs",
            "https://example.invalid/babs.git@fix",
        ],
        monkeypatch,
    )
    assert seen["mechababs"] == "/checkout@wip"
    assert seen["babs"] == "https://example.invalid/babs.git@fix"


def test_cli_requires_a_scratch_path(monkeypatch, capsys):
    """No campaign to derive one from, and defaulting to home or /tmp would put RIA
    stores where a cluster cannot carry them. So it is asked for."""
    from mechababs import cli

    monkeypatch.setattr(
        sys, "argv", ["mechababs", "test-cluster", "--cluster", "x.yaml"]
    )
    with pytest.raises(SystemExit):
        cli.main()
    assert "--scratch-path" in capsys.readouterr().err
