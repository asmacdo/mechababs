#!/usr/bin/env python3
"""Pre-flight checks before running mriqc on a dataset.

Usage: preflight.py <dataset_id>
  e.g. preflight.py ds005256

Runs checks (fail fast if any fail).
Exit 0 = good to go, exit 1 = should not proceed.
"""

import subprocess
import sys


def check_no_mriqc_repo(dataset_id):
    """Check that no mriqc derivative repo exists on GitHub.

    TODO: false-passes on `git ls-remote` failures. Returncode != 0 +
    empty stdout currently returns "no derivative" — but that case
    includes auth failures (no ssh-agent, encrypted-key with no
    passphrase entered), network errors, and any other git failure.
    Should distinguish "Repository not found" in stderr (legit
    pass) from auth/network errors (fail preflight). Also: only
    checks mriqc; with multiple pipelines should be pipeline-aware.
    """
    url = f"git@github.com:OpenNeuroDerivatives/{dataset_id}-mriqc.git"
    result = subprocess.run(
        ["git", "ls-remote", url, "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return False, f"mriqc derivative already exists: {url}"
    return True, "no mriqc derivative repo found"


def check_terminal_multiplexer(dataset_id):
    """Check that we're running inside screen or tmux."""
    import os

    if os.environ.get("TMUX") or os.environ.get("STY"):
        return True, "running in tmux/screen"
    if os.environ.get("TERM", "").startswith("screen"):
        return True, "running in screen"
    return (
        False,
        "not running in screen or tmux — long-running jobs may be killed on disconnect",
    )


CHECKS = [
    ("terminal multiplexer", check_terminal_multiplexer),
    ("mriqc derivative absent", check_no_mriqc_repo),
]


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <dataset_id>", file=sys.stderr)
        print(f"  e.g. {sys.argv[0]} ds005256", file=sys.stderr)
        sys.exit(1)

    dataset_id = sys.argv[1].removeprefix("study-")

    print(f"Preflight checks for {dataset_id}\n")

    all_passed = True
    for name, check_fn in CHECKS:
        passed, msg = check_fn(dataset_id)
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}: {msg}")
        if not passed:
            all_passed = False

    if not all_passed:
        print("\nPreflight FAILED — do not proceed.", file=sys.stderr)
        sys.exit(1)

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
