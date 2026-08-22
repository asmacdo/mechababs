"""validate.py — the body of ``mechababs test-cluster``.

Validating a cluster config used to be a repo-dev operation: check the repo out,
``pip install -e '.[test]'``, export ``MECHABABS_E2E_WORKDIR`` and ``BABS_SPEC``,
then run pytest by hand. But a campaign already carries everything that setup
reconstructs — the pinned babs, an isolated venv, the packaged e2e scenario, and a
natural workdir — so validation belongs on the operate side, next to ``iterate``
and ``status``.

What it runs is the e2e scenario, which drives a whole campaign (configure ->
add-dataset -> iterate: scaffold -> submit -> merge) and asserts a real derivative
landed. That is a far stronger check than ``babs check-setup``: it proves this
cluster's config actually produces output, on this cluster's scheduler.

**It does not run in the campaign you point at.** The scenario configures a
campaign, registers a dataset, and retires a derivative, so running it in a
campaign holding real work would be destructive. Instead it *provisions a throwaway
campaign from your campaign's pins* — the same mechababs + babs — and works there.
So what the campaign supplies is the environment, not the workspace.
"""

import os
import subprocess
import sys
from pathlib import Path

from mechababs import testing

# The scenario's scratch space: the campaign's own parent, so the throwaway campaign
# lands beside it on the same (fast, large) filesystem the campaign already lives on,
# and the container shim resolves as its sibling `../repronim-containers-shim`.
WORKDIR_ENV = "MECHABABS_E2E_WORKDIR"

# The container shim the scenario's pipelines resolve as a campaign sibling. It is
# host-prep, built once, and its absence is the likeliest reason a first run has
# nothing to do — so it is checked before pytest starts rather than surfacing as a
# skip (see `run_test_cluster`). Drops when PennLINC/babs#383 lands.
SHIM_DIRNAME = "repronim-containers-shim"


class RefError(Exception):
    """A vendored clone is not on anything bootstrap could re-clone."""


def clone_ref(clone):
    """The branch or tag a vendored code pin is on, as something re-clonable.

    bootstrap.sh pins with `git clone --branch <ref>`, which takes a branch or a tag,
    so the ref handed back has to be one of those. A tag pin leaves the clone on a
    detached HEAD, where `rev-parse --abbrev-ref HEAD` reports the literal "HEAD";
    passing that on fails deep inside bootstrap with "Remote branch HEAD not found".
    So fall back to the exact tag, and raise when it is neither.

    Lives here rather than in the e2e conftest so it is unit-testable against real
    repos. The wrappers apply the same branch-else-exact-tag rule to the checkout in
    shell, since they run before there is a venv to import this from.
    """
    ref = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    if ref != "HEAD":
        return ref
    tag = subprocess.run(
        ["git", "-C", str(clone), "describe", "--tags", "--exact-match"],
        text=True,
        capture_output=True,
    )
    if tag.returncode == 0 and tag.stdout.strip():
        return tag.stdout.strip()
    raise RefError(
        f"{clone} is on a detached HEAD with no exact tag, so there is no branch or "
        f"tag for bootstrap to re-clone; check out a branch or tag first"
    )


def default_workdir(campaign):
    """Where to build the scenario's campaign: beside the one under test.

    The pipelines resolve their container shim relative to the campaign root
    (`../repronim-containers-shim`), so the throwaway campaign has to share a parent
    with the shim — which is exactly how the campaign under test is already laid out.
    """
    return campaign.parent


def resolve_cluster(campaign, arg):
    """The cluster config to validate, as a path that exists.

    A path is taken as given; a bare name is looked up in the campaign's own
    ``clusters/`` (where ``configure`` copies configs, so the campaign owns them).
    Deliberately NOT resolved against a vendored clone's ``examples/`` — that path
    disappears when the code stops being cloned into the campaign.
    """
    candidate = Path(arg).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    in_campaign = campaign / "clusters" / arg
    if in_campaign.is_file():
        return in_campaign.resolve()
    # A campaign's clusters/ holds only a README until `configure` copies a config in,
    # and validating BEFORE processing real data is the point — so a path is the
    # expected form here, and the name form is the convenience after configure.
    sys.exit(
        f"cluster config not found: {arg}\n"
        f"pass the path to the config you want to validate, or the name of one already "
        f"in {campaign / 'clusters'} (which `configure` populates)"
    )


def pytest_command(cluster, campaign, extra_args=()):
    """The pytest invocation: the venv's own pytest over the packaged suite.

    Uses ``sys.executable -m pytest`` rather than a bare ``pytest`` so the run cannot
    drift to an ambient interpreter — the same reason ``construct`` invokes the venv's
    datalad by path. ``-s`` streams the scenario's phase logging, which matters when a
    step waits on real scheduler jobs.
    """
    return [
        sys.executable,
        "-m",
        "pytest",
        "-s",
        # The suite resolves inside the campaign's own code pin, and pytest would drop
        # a .pytest_cache/ there. That is untracked content inside a pinned subdataset,
        # which `guard.require_clean_pins` reads as a dirty pin — so validating a
        # cluster would leave every later command refusing to run. Keep both caches out.
        "-p",
        "no:cacheprovider",
        str(testing.suite_path()),
        "--cluster-config",
        str(cluster),
        "--campaign",
        str(campaign),
        *extra_args,
    ]


def require_shim(workdir):
    """Refuse before pytest starts if the container shim is missing.

    Otherwise the scenario's `simbids_sif` fixture skips, pytest exits 0, and
    `test-cluster` reports success having validated nothing — the worst outcome for a
    validation command. Mirrors the guard `run_on_cluster.sh` already applies.
    """
    shim = workdir / SHIM_DIRNAME
    if not (shim / ".datalad").is_dir():
        sys.exit(
            f"no container shim at {shim}\n"
            f"build it once (host prep):\n"
            f"    REPRONIM={shim} tmp-repronim-container-shim.sh bids-simbids"
        )


def run_test_cluster(campaign, cluster_arg, extra_args=(), workdir=None):
    """Validate a cluster config from the campaign; return an exit code."""
    cluster = resolve_cluster(campaign, cluster_arg)
    workdir = (
        Path(workdir).expanduser().resolve() if workdir else default_workdir(campaign)
    )
    workdir.mkdir(parents=True, exist_ok=True)
    require_shim(workdir)

    env = {
        **os.environ,
        WORKDIR_ENV: str(workdir),
        # pytest writes bytecode next to the suite, which sits in the campaign's
        # code pin; same dirty-pin problem as the cache above.
        "PYTHONPYCACHEPREFIX": str(workdir / ".pycache"),
    }
    cmd = pytest_command(cluster, campaign, extra_args)
    print(f"validating {cluster.name} on this cluster", file=sys.stderr)
    print(f"  scenario workdir: {workdir}", file=sys.stderr)
    print("+ " + " ".join(cmd), file=sys.stderr)
    code = subprocess.run(cmd, env=env, cwd=str(campaign)).returncode
    verdict = "PASSED" if code == 0 else "FAILED"
    print(f"\ncluster validation {verdict}: {cluster.name}", file=sys.stderr)
    if code == 0:
        print(
            f"Next: mechababs configure --cluster {cluster} --pipelines <…>",
            file=sys.stderr,
        )
        print(
            "(validating does not adopt the config; configure copies it in)",
            file=sys.stderr,
        )
    return code
