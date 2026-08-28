"""validate.py — the body of ``mechababs test-cluster``.

Validating a cluster config used to be a repo-dev operation: check the repo out,
``pip install -e '.[test]'``, export ``MECHABABS_E2E_WORKDIR``, then run pytest by
hand. ``test-cluster`` is that, as a command — so validation sits on the operate
side, next to ``iterate`` and ``status``.

What it runs is the packaged e2e scenario, which drives the whole spine
(``campaign init`` -> ``add-dataset`` -> ``iterate``: scaffold -> submit -> merge)
against a real scheduler and asserts a real derivative landed. That is a far
stronger check than ``babs check-setup``: it proves this cluster's config actually
produces output, on this cluster's scheduler.

**It recreates the environment it was called from, in a throwaway study.** A
campaign lives *inside* a study, so there is no standalone campaign to point at or
stand in — the scenario's fixtures build a fixture study and run ``campaign init``
inside it. Real studies are never touched.

The two pins get there differently, and the asymmetry is the point:

- **mechababs mirrors the caller.** With no ``--mechababs``, ``campaign init`` pins
  whichever mechababs is running it (read from PEP 610 install metadata), so the
  fixture campaign records the code being validated.
- **babs cannot mirror the caller.** babs is not a mechababs dependency; it is a
  dependency of the *generated campaign*, frozen by that campaign's lock. So the
  fixture campaign gets what a user's campaign would get.

Both are overridable — ``--mechababs URL@REF``, ``--babs URL@REF`` — which is how a
branch gets tested.
"""

import os
import subprocess
import sys
from pathlib import Path

from mechababs import testing

# Where the scenario builds its fixture studies, the container dataset it resolves
# as their sibling, and its caches. The CLI's `--scratch-path` sets it; the suite's
# `workdir` fixture reads it.
WORKDIR_ENV = "MECHABABS_E2E_WORKDIR"


class RefError(Exception):
    """A checkout is not on anything ``git clone --branch`` could re-clone."""


def clone_ref(clone):
    """The branch or tag a checkout is on, as something re-clonable.

    A code pin is set with `git clone --branch <ref>`, which takes a branch or a tag,
    so the ref handed back has to be one of those. A tag pin leaves the clone on a
    detached HEAD, where `rev-parse --abbrev-ref HEAD` reports the literal "HEAD";
    passing that on fails later with "Remote branch HEAD not found". So fall back to
    the exact tag, and raise when it is neither.
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
        f"tag to re-clone; check out a branch or tag first"
    )


def resolve_cluster(arg):
    """The cluster config to validate, as a path that exists.

    Path-or-URL, never a bare name — the same rule `campaign_init.stage_config`
    applies to every user-provided config. It has to hold here especially: the config
    being validated is by definition one no campaign has adopted yet, so there is
    nowhere a name could be looked up.
    """
    candidate = Path(arg).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    sys.exit(
        f"cluster config not found: {arg}\n"
        f"pass the path to the config you want to validate — cluster configs are "
        f"given by path, not by a name mechababs looks up."
    )


def pytest_command(cluster, extra_args=(), mechababs=None, babs=None):
    """The pytest invocation: this interpreter's pytest over the packaged suite.

    Uses ``sys.executable -m pytest`` rather than a bare ``pytest`` so the run cannot
    drift to an ambient interpreter — which also means pytest must be installed beside
    the mechababs running this, hence the ``[test]`` extra in the documented
    invocation. It cannot come from the fixture campaign's venv: the scenario's first
    act is ``campaign init``, so that campaign does not exist when pytest starts.

    ``-s`` streams the scenario's phase logging, which matters when a step waits on
    real scheduler jobs.

    A pin is passed only when the caller named one. Omitting ``--mechababs`` is what
    makes the fixture campaign self-pin to the mechababs running this command.
    """
    return [
        sys.executable,
        "-m",
        "pytest",
        "-s",
        # The suite ships inside the installed package, so pytest would drop a
        # .pytest_cache/ beside it — inside a checkout, for an editable install.
        # Keep both caches out (PYTHONPYCACHEPREFIX below is the other half).
        "-p",
        "no:cacheprovider",
        str(testing.suite_path()),
        "--cluster-config",
        str(cluster),
        *(["--mechababs", mechababs] if mechababs else []),
        *(["--babs", babs] if babs else []),
        *extra_args,
    ]


def run_test_cluster(
    cluster_arg, scratch_path, extra_args=(), mechababs=None, babs=None
):
    """Validate a cluster config on this cluster; return an exit code."""
    cluster = resolve_cluster(cluster_arg)
    scratch = Path(scratch_path).expanduser().resolve()
    scratch.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        WORKDIR_ENV: str(scratch),
        # pytest writes bytecode next to the suite, which for an editable install
        # means inside a checkout; same problem as the cache above.
        "PYTHONPYCACHEPREFIX": str(scratch / ".pycache"),
    }
    cmd = pytest_command(cluster, extra_args, mechababs=mechababs, babs=babs)
    print(f"validating {cluster.name} on this cluster", file=sys.stderr)
    print(f"  scenario scratch: {scratch}", file=sys.stderr)
    print("+ " + " ".join(cmd), file=sys.stderr)
    # Run from the scratch path rather than wherever the user stood: invoked from a
    # checkout, the repo's own `testpaths = ["tests"]` would otherwise pull in the
    # unit suite alongside the scenario.
    code = subprocess.run(cmd, env=env, cwd=str(scratch)).returncode
    verdict = "PASSED" if code == 0 else "FAILED"
    print(f"\ncluster validation {verdict}: {cluster.name}", file=sys.stderr)
    if code == 0:
        print(
            f"Next, from inside the study you want to process: "
            f"mechababs campaign init <label> --cluster {cluster} --apps <…>",
            file=sys.stderr,
        )
        print(
            "(validating does not adopt the config; campaign init copies it in)",
            file=sys.stderr,
        )
    return code
