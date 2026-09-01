"""testing — the e2e scenario, shipped with the package.

The e2e suite drives the whole spine (``campaign init`` -> ``add-dataset`` ->
``iterate``: scaffold -> submit -> merge) against a fixture study and asserts a
real derivative landed, which makes it the executable specification of "this
cluster config works". ``mechababs test-cluster`` runs it, so the suite has to be
reachable wherever mechababs is *installed*, not only in a source checkout.

That is why it lives inside the package. It ships in the distribution and is
located through ``importlib``, never by a path into a checkout — so it resolves
whether mechababs was installed from a pinned git ref, from a release, or
editable from a working tree.

``tests/`` keeps the unit suite: it tests the code and never leaves the repo, so
it has no reason to travel.
"""

from importlib.resources import files
from pathlib import Path

E2E_DIRNAME = "e2e"

# The scenario modules, used to sanity-check that the directory we resolved is
# actually the suite (a partial install would otherwise fail later, inside pytest).
# Every module that ships is listed: a test file present in the distribution but
# absent from here would be silently skipped by a truncated install.
SUITE_MODULES = (
    "conftest.py",
    "test_spine.py",
    "test_study_fixture.py",
    "test_superstudy.py",
)


def suite_path():
    """The directory holding the packaged e2e suite, as a real filesystem path.

    pytest collects from a path rather than an import, so this resolves the
    package's own location instead of importing the test modules. Raises if the
    suite is missing, which means mechababs was installed without its scenario
    (see ``package-data`` in ``pyproject.toml``) — a clearer failure than pytest
    reporting "no tests collected".
    """
    path = Path(files("mechababs.testing") / E2E_DIRNAME)
    missing = [name for name in SUITE_MODULES if not (path / name).is_file()]
    if missing:
        raise RuntimeError(
            f"packaged e2e suite incomplete at {path} (missing: {', '.join(missing)}). "
            "mechababs was installed without its test scenario; reinstall from a "
            "distribution that includes mechababs/testing/e2e/."
        )
    return path
