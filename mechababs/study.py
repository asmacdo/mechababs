"""study.py — the study a mechababs command operates in.

mechababs operates on a BIDS study that **already exists** on disk —
cloned, or authored by another tool — and never creates one (docs/spec.md,
"Layout & input"). Every command therefore begins by answering the same question,
so it is answered in exactly one place here.

The check is deliberately shallow: a study root is a datalad dataset (or, for a
fixture, a plain git repo). It is not "does this look like valid BIDS" — the
commands that need real study content (``add-dataset`` reading the per-subject
metadata TSV, scaffold reading ``sourcedata/``) fail on the thing they actually
need, with a message about that thing. What this module protects against is the
much commoner mistake of running from the wrong directory entirely.
"""

import sys
from pathlib import Path


def is_study_root(path):
    """True if ``path`` is a dataset root: datalad, or plain git for a fixture.

    ``.git`` is tested with ``exists()`` rather than ``is_dir()`` so a git
    worktree (whose ``.git`` is a file) counts.
    """
    path = Path(path)
    return (path / ".datalad").is_dir() or (path / ".git").exists()


def require_study_root(path="."):
    """Resolve ``path`` and exit unless it is a study root."""
    study = Path(path).resolve()
    if not is_study_root(study):
        sys.exit(
            f"not a study (no datalad/git dataset here): {study}\n"
            "mechababs operates inside an existing BIDS study — cd into one, or "
            "clone one first (mechababs does not create studies)."
        )
    return study
