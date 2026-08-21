"""campaign.py — a campaign's layout, its selection, and its env guard.

A campaign is a **config epoch**, not a dataset: one pinned environment, one bundle
of BIDS-App configs, one cluster, and the state of that root's cells under it. It
lives at ``<root>/.mechababs/campaigns/<label>/`` (docs/output_structure.md), and a
root accumulates campaigns over time — a set of derivatives now, another a year
later with newer tools, each its own ``<label>``.

``root`` throughout this module is the **operating-level root**: the study or the
superstudy the campaign is configured at, whose footprint is identical either way.
``study`` is reserved for parameters that must be a lone or member study — only
``state_path``, since a statefile exists only at a study.

```
<root>/.mechababs/campaigns/<label>/
  campaign.yaml               the app bundle (ordered) + cluster choice + limit
  bids-app-configs/           the app configs, copied in
  clusters/                   the cluster config, copied in
  env.sh                      source to select this campaign + activate its venv
  pyproject.toml              declares mechababs + babs
  uv.lock                     the resolved environment — the provenance record
  sourcedata+derivatives.tsv  the statefile: this study's cells (at a study only)
  inclusions/                 the requested subject list per cell, pinned at scaffold
  .venv/                      gitignored, rebuilt from the lock
```

Two rules this module enforces for every verb that comes later:

**Selection is always the env var.** ``MECHABABS_CAMPAIGN`` names the label, and
sourcing the campaign's ``env.sh`` is what sets it. There is no default-if-only-one
shortcut and no ``--campaign`` flag, so one campaign and five behave identically.

**The running venv must match the committed lock.** The lock is mutable through git
history (that is how a mid-sweep version bump works), so "the venv I am running in"
and "the environment this campaign records" can drift apart in either direction.
``require_env_match`` refuses both directions rather than letting a run be recorded
against tools that did not produce it.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

from mechababs import study as study_mod

MECHABABS_DIR = ".mechababs"
CAMPAIGNS_DIRNAME = "campaigns"

CONFIG_FILENAME = "campaign.yaml"
STATE_FILENAME = "sourcedata+derivatives.tsv"
APPS_DIRNAME = "bids-app-configs"
CLUSTERS_DIRNAME = "clusters"
INCLUSIONS_DIRNAME = "inclusions"
ENV_FILENAME = "env.sh"
PYPROJECT_FILENAME = "pyproject.toml"
LOCK_FILENAME = "uv.lock"
VENV_DIRNAME = ".venv"

# Written into the venv (so it is gitignored and per-environment by construction)
# at build time, and read by the env-match guard. See `write_env_stamp`.
STAMP_FILENAME = ".mechababs-env.json"

CAMPAIGN_ENV_VAR = "MECHABABS_CAMPAIGN"

# The statefile is TALL: one row per (source dataset x app config) cell.
#   identity  — inputs, written at add-dataset, never overwritten
#   topology  — derived from the app config
#   derived   — reconciled each tick; state is READ OFF these, there is no status
#               enum (`babs` empty -> scaffold; set + `merged` empty -> active;
#               `merged` set -> done). Volatile job status stays in babs.
IDENTITY_COLUMNS = ["source_dataset", "app_config", "processing_level",
                    "n_subjects", "n_sessions"]
TOPOLOGY_COLUMNS = ["depends_on"]
DERIVED_COLUMNS = ["babs", "merged"]
STATE_COLUMNS = IDENTITY_COLUMNS + TOPOLOGY_COLUMNS + DERIVED_COLUMNS


def campaigns_dir(root):
    return Path(root) / MECHABABS_DIR / CAMPAIGNS_DIRNAME


def campaign_dir(root, label):
    return campaigns_dir(root) / label


def config_path(root, label):
    return campaign_dir(root, label) / CONFIG_FILENAME


def state_path(study, label):
    """``study``, not ``root``: a statefile exists only at a study.

    The one asymmetry in the campaign footprint. A superstudy's campaign dir carries
    membership instead — per-cell state shards to the member studies, and the
    superstudy computes its rollup from them.
    """
    return campaign_dir(study, label) / STATE_FILENAME


def apps_dir(root, label):
    return campaign_dir(root, label) / APPS_DIRNAME


def clusters_dir(root, label):
    return campaign_dir(root, label) / CLUSTERS_DIRNAME


def inclusions_dir(root, label):
    return campaign_dir(root, label) / INCLUSIONS_DIRNAME


def env_path(root, label):
    return campaign_dir(root, label) / ENV_FILENAME


def pyproject_path(root, label):
    return campaign_dir(root, label) / PYPROJECT_FILENAME


def lock_path(root, label):
    return campaign_dir(root, label) / LOCK_FILENAME


def venv_path(root, label):
    """The campaign's venv — one venv per campaign, beside the lock it was built from.

    This is where ``uv sync --project <campaign-dir>`` puts it, which is what lets
    ``env.sh`` be committed: the path is derivable from the campaign dir, not
    recorded anywhere.
    """
    return campaign_dir(root, label) / VENV_DIRNAME


def initial_header():
    """The header line of a fresh statefile — no rows; add-dataset writes those."""
    return "\t".join(STATE_COLUMNS) + "\n"


def lock_digest(lock_text):
    """The identity of a lock's *content* (what the env-match guard compares)."""
    return hashlib.sha256(lock_text.encode()).hexdigest()


def write_env_stamp(venv, label, lock_text):
    """Record which lock the venv at ``venv`` was built from.

    Lives inside the venv on purpose: the venv is gitignored and rebuilt, so the
    stamp can never be committed, and it cannot outlive the environment it
    describes. ``campaign update-env`` rewrites it when it rebuilds.
    """
    stamp = Path(venv) / STAMP_FILENAME
    stamp.write_text(json.dumps(
        {"label": label, "lock_sha256": lock_digest(lock_text)}, indent=2) + "\n")
    return stamp


def read_env_stamp(venv):
    """The venv's stamp, or None if it has none (not built by mechababs)."""
    stamp = Path(venv) / STAMP_FILENAME
    if not stamp.is_file():
        return None
    try:
        return json.loads(stamp.read_text())
    except json.JSONDecodeError:
        return None


def selected_label():
    """The campaign named by ``MECHABABS_CAMPAIGN``; exit if unset.

    Selection is *always* explicit — no default-if-only-one — so the habit a user
    forms on a one-campaign study is the one that still works on a five-campaign one.
    """
    label = os.environ.get(CAMPAIGN_ENV_VAR, "").strip()
    if not label:
        sys.exit(
            f"no campaign selected ({CAMPAIGN_ENV_VAR} is unset).\n"
            f"Source a campaign's env.sh to select it and activate its venv:\n"
            f"  source {MECHABABS_DIR}/{CAMPAIGNS_DIRNAME}/<label>/{ENV_FILENAME}"
        )
    return label


def require_env_match(root, label):
    """Refuse unless this process is running the environment the campaign records.

    Two failures, both of which would attribute a run to tools that did not produce
    it: running from some *other* python (an ambient install, another campaign's
    venv), and running from this campaign's venv after the committed lock moved
    (or before a bumped lock was built). The fix for the second is
    ``mechababs campaign update-env``, which the message names.
    """
    campaign = campaign_dir(root, label)
    if not config_path(root, label).is_file():
        sys.exit(f"no campaign {label!r} here (looked for "
                 f"{config_path(root, label)})")

    venv = venv_path(root, label).resolve()
    prefix = Path(sys.prefix).resolve()
    if prefix != venv:
        sys.exit(
            f"not running in the venv of campaign {label!r}\n"
            f"  expected: {venv}\n"
            f"  running:  {prefix}\n"
            f"Source the campaign's env.sh:\n"
            f"  source {env_path(root, label)}"
        )

    lock = lock_path(root, label)
    if not lock.is_file():
        sys.exit(f"campaign {label!r} has no {LOCK_FILENAME} ({lock})")
    committed = lock_digest(lock.read_text())
    stamp = read_env_stamp(venv)
    if stamp is None or stamp.get("lock_sha256") != committed:
        sys.exit(
            f"the venv of campaign {label!r} does not match its committed "
            f"{LOCK_FILENAME}\n"
            "The lock and the environment have drifted — either the lock was "
            "bumped and the venv not rebuilt, or the venv was built from a lock "
            "that is no longer committed.\n"
            f"Rebuild it:  mechababs campaign update-env\n"
            f"  lock:  {lock}\n"
            f"  venv:  {venv}"
        )
    return campaign


def require_selected_campaign(path="."):
    """The three preconditions every *operating* verb shares, in one call.

    At a study root (``require_study_root``), with a campaign selected
    (``selected_label``), running the environment that campaign records
    (``require_env_match``). Returns ``(root, label, campaign_dir)``.

    ``campaign init`` is the one command that does not take this: it runs before
    the environment exists — it is what creates it.
    """
    root = study_mod.require_study_root(path)
    label = selected_label()
    return root, label, require_env_match(root, label)
