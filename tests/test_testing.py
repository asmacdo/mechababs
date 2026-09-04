"""Tests for `mechababs.testing` — locating the packaged e2e suite.

The point of these: `mechababs test-cluster` has to find the suite through the
INSTALLED package, not through a path into a clone — the campaign references and
locks the code, it never vendors it.
"""

import mechababs.testing as testing
import pytest


def test_suite_path_resolves_to_the_packaged_suite():
    path = testing.suite_path()
    assert path.is_dir(), f"packaged suite not a directory: {path}"
    for name in testing.SUITE_MODULES:
        assert (path / name).is_file(), f"{name} missing from {path}"


def test_suite_path_lives_inside_the_package():
    """Not a `code/<clone>/tests/e2e` path: it must sit under mechababs/testing/,
    which is what makes it travel with an install."""
    path = testing.suite_path()
    assert path.name == testing.E2E_DIRNAME
    assert path.parent.name == "testing"
    assert path.parent.parent.name == "mechababs"


def test_suite_path_reports_an_incomplete_install(monkeypatch, tmp_path):
    """A distribution built without the scenario should fail with a clear message
    rather than letting pytest report "no tests collected" later on."""
    monkeypatch.setattr(testing, "files", lambda _package: tmp_path)
    (tmp_path / testing.E2E_DIRNAME).mkdir()
    with pytest.raises(RuntimeError, match="packaged e2e suite incomplete"):
        testing.suite_path()


def _setuptools_config():
    import tomllib
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    return tomllib.loads((repo / "pyproject.toml").read_text())["tool"]["setuptools"]


def test_the_scenario_is_declared_as_package_data():
    """The suite only travels if pyproject ships it; guard the packaging itself so a
    stray edit to `packages`/`package-data` cannot silently un-ship it."""
    setuptools = _setuptools_config()
    assert "mechababs.testing" in setuptools["packages"]
    patterns = setuptools["package-data"]["mechababs.testing"]
    assert any(
        p.startswith(f"{testing.E2E_DIRNAME}/") and p.endswith(".py") for p in patterns
    )


def test_the_dev_wrapper_script_is_excluded_from_the_distribution():
    """The wrapper drives the suite from a checkout, so it must not ship.

    This needs an explicit exclude: setuptools_scm's file finder plus the default
    include-package-data would otherwise ship every git-tracked file under the package,
    which makes the package-data globs additive rather than restrictive.
    """
    excluded = _setuptools_config()["exclude-package-data"]["mechababs.testing"]
    assert f"{testing.E2E_DIRNAME}/*.sh" in excluded


def _scenario_conftest():
    """The packaged conftest, loaded by path.

    `e2e/` ships as package DATA, not as an importable subpackage, so there is no
    `mechababs.testing.e2e.conftest` to import — and loading it by path is also how
    pytest itself picks it up.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_scenario_conftest", testing.suite_path() / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture_body(fixture):
    """The plain function inside a `@pytest.fixture` (wrapped since pytest 8.4)."""
    return getattr(fixture, "__wrapped__", fixture)


class _NoOptions:
    """A request whose options are all unset — a bare `pytest <suite>` invocation."""

    class config:
        @staticmethod
        def getoption(_name):
            return None


def test_the_scenario_refuses_to_run_without_the_config_under_test():
    """The one required option must raise, not skip.

    pytest exits 0 when every test skips, so a skip here would let `test-cluster`
    report success having validated nothing — the worst outcome for a validation
    command. That guarantee is the reason this is a `UsageError`, so it gets a test.
    """
    conftest = _scenario_conftest()
    body = _fixture_body(conftest.cluster_config)
    with pytest.raises(pytest.UsageError, match="required"):
        body(_NoOptions())


def test_an_unset_mechababs_pin_means_let_campaign_init_self_pin():
    """Unset is a real mode, not a missing input: `campaign init` then pins whichever
    mechababs is running it, which under `test-cluster` is the code under test."""
    conftest = _scenario_conftest()
    assert _fixture_body(conftest.mechababs_pin)(_NoOptions()) is None
