"""Tests for `mechababs.testing` — locating the packaged e2e suite.

The point of these: `mechababs test-cluster` has to find the suite through the
INSTALLED package, not through a path into a vendored clone, so it keeps working
when the code stops being cloned into the campaign (con/mechababs#101).
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
    assert any(p.startswith(f"{testing.E2E_DIRNAME}/") and p.endswith(".py") for p in patterns)


def test_the_dev_wrapper_scripts_are_excluded_from_the_distribution():
    """The wrapper scripts drive the suite from a checkout, so they must not ship.

    This needs an explicit exclude: setuptools_scm's file finder plus the default
    include-package-data would otherwise ship every git-tracked file under the package,
    which makes the package-data globs additive rather than restrictive.
    """
    excluded = _setuptools_config()["exclude-package-data"]["mechababs.testing"]
    assert f"{testing.E2E_DIRNAME}/*.sh" in excluded
