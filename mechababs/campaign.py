"""campaign.py — a campaign's layout, its selection, and its env guard.

A campaign is not a dataset. It is one pinned environment, one bundle
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
against tools that did not produce it. mechababs keeps no environment metadata of
its own: the check is delegated whole to ``uv sync --check`` (see
``venv_matches_lock``), so the only environment artifacts are the lock and the venv.
"""

import csv
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import yaml

from mechababs import study as study_mod

MECHABABS_DIR = ".mechababs"
CAMPAIGNS_DIRNAME = "campaigns"

CONFIG_FILENAME = "campaign.yaml"
STATE_FILENAME = "sourcedata+derivatives.tsv"
# The single-writer flock. The writer unit is the study or superstudy, not the
# campaign (campaigns coexist under one level but never operate at once), so it is
# one per level, directly under .mechababs/, and gitignored from there since a lock
# left in the tree would dirty the study every `iterate`.
FLOCK_FILENAME = ".single-writer.flock"
# The superstudy's counterpart to the statefile: a study's campaign dir carries
# per-cell STATE, a superstudy's carries MEMBERSHIP, and the rollup is computed on
# demand so there is no master copy to drift from what it summarizes.
MEMBERS_FILENAME = "studies+sourcedata.tsv"
APPS_DIRNAME = "bids-app-configs"
CLUSTERS_DIRNAME = "clusters"
INCLUSIONS_DIRNAME = "inclusions"
ENV_FILENAME = "env.sh"
PYPROJECT_FILENAME = "pyproject.toml"
UV_LOCK_FILENAME = "uv.lock"
VENV_DIRNAME = ".venv"

CAMPAIGN_ENV_VAR = "MECHABABS_CAMPAIGN"

# Where datalad commits a dataset's identity, and the key it uses. Read directly
# (see `dataset_id`) so the answer does not depend on the dataset being initialized.
MECHABABS_DATALAD_CONFIG = Path(".datalad") / "config"
DATALAD_ID_KEY = "datalad.dataset.id"

# The statefile is TALL: one row per (source dataset x app config) cell.
#   identity  — inputs, written at add-dataset, never overwritten
#   topology  — derived from the app config
#   derived   — reconciled each iterate; state is READ OFF these, there is no status
#               enum (`babs` empty -> scaffold; set + `merged` empty -> active;
#               `merged` set -> done). Volatile job status stays in babs.
IDENTITY_COLUMNS = [
    "source_dataset",
    "app_config",
    "processing_level",
    "n_subjects",
    "n_sessions",
]
TOPOLOGY_COLUMNS = ["depends_on"]
DERIVED_COLUMNS = ["babs", "merged"]
STATE_COLUMNS = IDENTITY_COLUMNS + TOPOLOGY_COLUMNS + DERIVED_COLUMNS

# The superstudy's membership catalog: which (study, source dataset) pairs this
# campaign runs on, plus the ONE piece of state committed at the super. It exists for
# readers who have git but not the cluster; per-cell truth stays in the member shards.
#
# One lifecycle per ROW, not per member: the file is `studies+sourcedata.tsv` and a
# study may carry more than one source dataset, each with its own progress. The finer
# grain computes the coarser one for free (a study is done when all its rows are), so
# rolling up here would only lose which source dataset is behind.
MEMBER_COLUMNS = ["study", "source_dataset", "lifecycle"]

# The three a row can read, in the order they happen. `registered` is what
# `add-dataset` writes: selected into the campaign, nothing dispatched (not
# "pending": nothing is queued, and it would collide with the cell vocabulary's
# `waiting`). `merged` is the word the cell table uses for the same fact one grain
# down, so a member and its cells read alike.
LIFECYCLE_REGISTERED = "registered"
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_MERGED = "merged"

# The member's half of the superstudy relationship, written into its campaign.yaml
# when its footprint is created. Its value is the super's DATALAD-ID: an identity,
# not a location (see `superstudy_of` for why, and for turning it back into a place).
SUPERSTUDY_KEY = "superstudy"


def campaigns_dir(root):
    return Path(root) / MECHABABS_DIR / CAMPAIGNS_DIRNAME


def campaign_dir(root, label):
    return campaigns_dir(root) / label


def config_path(root, label):
    return campaign_dir(root, label) / CONFIG_FILENAME


def declared_app_stems(root, label):
    """The stems of every app this campaign declares, from ``campaign.yaml``.

    The campaign's **vocabulary**, and the thing to validate an app name against.
    Read at the level the campaign is configured at, so it is answerable whatever is
    or is not installed below it: a superstudy whose members have all been pushed and
    uninstalled still knows exactly which apps it runs, while its cells are
    unreadable. Validating a filter against visible cells instead would let a typo
    through in precisely that case, and report it as "nothing to see".

    The bundle is fixed at ``campaign init`` and added whole, so this is the complete
    set of app stems any of this campaign's cells can carry.
    """
    config = yaml.safe_load(config_path(root, label).read_text()) or {}
    return sorted({Path(rel).stem for rel in (config.get("apps") or [])})


def require_declared_app(root, label, app):
    """Exit unless ``app`` names one of this campaign's declared apps.

    One refusal shared by every command that narrows by app, so the message a typo
    gets does not depend on which verb you typed it into.
    """
    stems = declared_app_stems(root, label)
    if app not in stems:
        sys.exit(
            f"--app {app!r} is not an app in campaign {label!r}.\n"
            f"This campaign's apps are: {', '.join(stems) or '(none)'}"
        )


def state_path(study, label):
    """``study``, not ``root``: a statefile exists only at a study.

    The one asymmetry in the campaign footprint. A superstudy's campaign dir carries
    membership instead — per-cell state shards to the member studies, and the
    superstudy computes its rollup from them.
    """
    return campaign_dir(study, label) / STATE_FILENAME


def members_path(superstudy, label):
    """``superstudy``, not ``root``: a membership catalog exists only at a super.

    The mirror image of :func:`state_path`. A campaign dir has one or the other,
    never both, and which one it has *is* the record of the level it was
    configured at.
    """
    return campaign_dir(superstudy, label) / MEMBERS_FILENAME


def initial_members_header():
    """The header line of a fresh membership catalog — no rows; add-dataset writes those."""
    return "\t".join(MEMBER_COLUMNS) + "\n"


def is_superstudy_campaign(root, label):
    """True if ``label`` at ``root`` is configured at superstudy level.

    Read from the layout rather than a config key: the campaign dir carries a
    membership catalog or a statefile, and that file's presence is the fact. A
    marker that could contradict the layout would just be a second source of
    truth for the same question.
    """
    return members_path(root, label).is_file()


def read_members(superstudy, label):
    """The catalog's rows, in file order."""
    with open(members_path(superstudy, label), newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def write_members(superstudy, label, rows):
    """Rewrite the catalog with ``MEMBER_COLUMNS`` and ``rows``, in order."""
    with open(members_path(superstudy, label), "w", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=MEMBER_COLUMNS, delimiter="\t", lineterminator="\n"
        )
        w.writeheader()
        for row in rows:
            w.writerow({c: row.get(c, "") for c in MEMBER_COLUMNS})


def dataset_id(path):
    """``path``'s datalad-id, or ``None`` if it has none.

    The id is stable across every commit and survives both cloning and relocating,
    which is what makes it an identity rather than a location.

    Read out of ``.datalad/config`` with git's own parser rather than through
    ``Dataset(path).id``, which needs a real git repository: the id is committed
    configuration, so reading the file answers the question directly and keeps the
    check honest where a dataset is present but not initialized.
    """
    config = Path(path) / MECHABABS_DATALAD_CONFIG
    if not config.is_file():
        return None
    out = subprocess.run(
        ["git", "config", "--file", str(config), "--get", DATALAD_ID_KEY],
        capture_output=True,
        text=True,
    )
    return out.stdout.strip() or None


def recorded_superstudy_id(study, label):
    """The datalad-id of the super this member's campaign belongs to, or ``None``.

    **Who owns it**, which is a different question from **where that owner is**
    (:func:`superstudy_of`). Ownership is answerable from the member alone, so it
    stays answerable when the owner is nowhere nearby — a member cloned out of one
    superstudy and into another still says whose campaign it carries, even though
    the original is not on this filesystem at all.
    """
    path = config_path(study, label)
    if not path.is_file():
        return None
    config = yaml.safe_load(path.read_text()) or {}
    return config.get(SUPERSTUDY_KEY) or None


def superstudy_of(study, label):
    """The super this member's campaign belongs to, or ``None`` if it is its own.

    The marker records the super's **datalad-id**, and this resolves it to a place
    by walking up from the member until a dataset carries that id.

    An id and not a path, because a relative path re-resolves wherever the member
    currently sits: a member cloned somewhere else would keep pointing at "one level
    up" and silently adopt whatever is there — a different superstudy passing the
    ownership check, or an arbitrary parent directory becoming its environment.

    The walk is not the parent-scanning the design forbids ("is there something
    above me that might claim this"). The member has already asserted *which*
    dataset its campaign belongs to; the walk only finds where that dataset
    currently lives, and any other ancestor is ignored.

    **Unresolvable means detached**, deliberately: a member that cannot find the
    super it names is on its own, whatever the reason, and operating on its own
    contents is what it is equipped for. The failure that follows is then an honest
    "no environment here" rather than an environment resolved at the wrong level.
    """
    recorded = recorded_superstudy_id(study, label)
    if not recorded:
        return None
    for candidate in Path(study).resolve().parents:
        if dataset_id(candidate) == recorded:
            return candidate
    return None


def operated_level(study, label):
    """The level ``study``'s campaign is operated from — its super, or itself.

    The one definition of a distinction the whole superstudy layer turns on. A
    campaign has two levels that coincide for a study and diverge for a member: the
    study is where the cells live and the work runs, while the level it was
    *configured* at is where the environment lives (venv, ``env.sh``, the lock that
    built it) and where the single writer is enforced.

    Ask this whenever the answer is about the campaign's environment or its
    serialization. Ask ``study`` itself whenever the answer is about the work — the
    shard, the derivatives, the member's own recorded lock epoch. A unit test cannot
    catch the two swapped: a test of a guard is written against the same assumption
    the guard makes, so the e2e superstudy scenario is what checks it.
    """
    return superstudy_of(study, label) or Path(study)


def require_statefile(study, label):
    """``study``'s statefile for ``label``, or exit saying why there is none.

    The one place the study/superstudy asymmetry is enforced rather than assumed.
    A superstudy's campaign dir carries membership and no cell state, so a verb
    that needs a shard — every reconciler transition — is being pointed at the
    wrong level, and that is a different mistake from a missing campaign.
    """
    path = state_path(study, label)
    if not path.is_file():
        if config_path(study, label).is_file():
            sys.exit(
                f"campaign {label!r} here has no {STATE_FILENAME} ({path}).\n"
                "Per-cell state lives in a study; a superstudy's campaign dir "
                "carries membership instead. Run this in the member study whose "
                "cell you mean to advance."
            )
        sys.exit(f"no campaign {label!r} here (looked for {config_path(study, label)})")
    return path


def flock_path(root):
    """The level's single-writer lock: one per study or superstudy, not per campaign."""
    return Path(root) / MECHABABS_DIR / FLOCK_FILENAME


def level_gitignore_path(root):
    """The ``.gitignore`` that hides the lock — at ``.mechababs/`` itself, one per level
    like the lock, shared by every campaign there."""
    return Path(root) / MECHABABS_DIR / ".gitignore"


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


def uv_lock_path(root, label):
    return campaign_dir(root, label) / UV_LOCK_FILENAME


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


def read_state(study, label):
    """The shard's cells, in file order — one dict per (source dataset x app) row.

    Row order is meaningful: ``iterate`` advances cells in it (spec, "Ordering is
    unchanged"), so readers and writers preserve it rather than sorting.
    """
    with open(state_path(study, label), newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def write_state(study, label, rows):
    """Rewrite the shard with ``STATE_COLUMNS`` and ``rows``, in order.

    The schema is the module's, not the file's: a tall statefile's columns do not
    vary with the campaign's app bundle.
    """
    with open(state_path(study, label), "w", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=STATE_COLUMNS, delimiter="\t", lineterminator="\n"
        )
        w.writeheader()
        for row in rows:
            w.writerow({c: row.get(c, "") for c in STATE_COLUMNS})


def cell_key(row):
    """A cell's identity: the (source dataset, app config) pair. Unique in a shard."""
    return (row["source_dataset"], row["app_config"])


def find_cell(rows, source_dataset, app_config):
    """The one row for this cell, or exit naming what was asked for.

    Every action verb starts here, which is why it lives beside ``read_state``
    rather than in any one of them: the shard is what they share.
    """
    for row in rows:
        if cell_key(row) == (source_dataset, app_config):
            return row
    sys.exit(
        f"no cell for ({source_dataset}, {app_config}) in this campaign's "
        f"statefile — `mechababs add-dataset` writes the cells, and an action "
        f"verb only advances one that is already there."
    )


def require_active_cell(row, verb):
    """Refuse unless the cell is ACTIVE — scaffolded, and not yet merged.

    The column routing (``babs`` empty -> scaffold; set with ``merged`` empty ->
    active; ``merged`` set -> done) read as a guard, for the two verbs that advance
    an active cell. It lives here because it is the statefile's semantics rather
    than either verb's.

    Self-guarding, in ``mechababs-inner``'s sense: these verbs are only ever reached
    because something decided the cell was in this state, so being wrong about that
    must be loud — a `datalad rerun` onto current HEAD lands right here.

    Returns the cell's ``babs`` path (study-relative), which is what both verbs
    drive babs against.
    """
    where = f"{row['source_dataset']} / {Path(row['app_config']).stem}"
    if not row.get("babs"):
        sys.exit(
            f"{where} is not scaffolded, so there are no jobs to {verb}.\n"
            "An empty `babs` column is the not-started state; scaffold is what "
            "advances it."
        )
    if row.get("merged"):
        sys.exit(
            f"{where} is already merged ({row['babs']}), so there is nothing to "
            f"{verb}.\nA merged cell is done. To redo it, retire the derivative — "
            "that resets the cell in the same act."
        )
    return row["babs"]


def babs_bin():
    """The pinned ``babs``: this environment's, never PATH's.

    A campaign's venv *is* its babs pin, and ``require_env_match`` is what vouches
    for ``sys.prefix`` being that venv. PATH can disagree with it (a stray
    user-level babs), and a run attributed to the recorded pin but produced by
    another babs is the failure the pinning exists to prevent.
    """
    return str(Path(sys.prefix) / "bin" / "babs")


def uv_bin():
    """The pinned ``uv``: this environment's, never PATH's.

    Same rule and same reason as :func:`babs_bin`: ``uv`` is a campaign dependency,
    so a venv built from the campaign's lock contains the ``uv`` that checks it, and
    a ``datalad rerun`` in a study cloned somewhere else needs nothing ambient.
    """
    return str(Path(sys.prefix) / "bin" / "uv")


def venv_matches_lock(campaign):
    """Ask uv whether the RUNNING environment is what ``campaign``'s lock describes.

    Returns ``(ok, detail)``, where ``detail`` is uv's own output for the caller to
    quote. The whole freshness check, delegated: mechababs records nothing about an
    environment and compares nothing itself.

    Three flags carry the design:

    - ``--check`` reports rather than installs, so a guard never mutates the
      environment it is vouching for;
    - ``--frozen`` uses the lock as committed, so the check can neither re-resolve
      nor rewrite it — a moved branch pin is not chased here (that is
      ``update-env --upgrade``'s explicit act);
    - ``--offline`` keeps it network-free, so the guard costs the same on a login
      node, a compute node, or a laptop with no route out.

    ``UV_PROJECT_ENVIRONMENT`` names the environment to check, deterministically.
    Without it uv ignores the active interpreter (with a warning) and checks the
    project's own ``.venv``, which for a member is not the venv doing the work; the
    ``--active`` flag would read the ambient ``VIRTUAL_ENV`` instead, and a process
    knows its own ``sys.prefix`` more surely than it knows what activated it.
    """
    proc = subprocess.run(
        [
            uv_bin(),
            "sync",
            "--check",
            "--frozen",
            "--offline",
            "--project",
            str(campaign),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": sys.prefix},
    )
    detail = (proc.stderr or proc.stdout or "").strip()
    return proc.returncode == 0, detail


def _quoted(detail):
    """uv's own output, indented under our explanation — or nothing if it said nothing.

    Kept as a tail rather than the message: uv answers "how does this environment
    differ from the lock", which is the evidence, while what a user needs first is
    which of their two environments is wrong and which command fixes it.
    """
    if not detail:
        return ""
    return "\n\nuv:\n" + "\n".join(f"  {line}" for line in detail.splitlines())


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

    Two checks, both refusing something that would attribute a run to tools that did
    not produce it. **Location**: this process is some *other* python (an ambient
    install, another campaign's venv) — the half only mechababs can answer, because
    only mechababs knows which campaign you meant. **Freshness**: the venv no longer
    agrees with the committed lock, delegated to ``uv sync --check`` against the real
    installed environment, so a bumped-but-unbuilt lock and a hand-``pip install``ed
    package fail alike. The fix for the second is ``mechababs campaign update-env``,
    which the message names.

    **The environment is resolved at the level the campaign is operated from, not
    at ``root``** (``operated_level``): a member of a super-campaign holds the cells
    but by construction has no venv of its own, and this is called with the member
    as ``root`` when the fan-out dispatches an inner verb there. The member's own
    lock copy is checked separately, by the inner verbs (``require_study_lock_match``).
    """
    campaign = campaign_dir(root, label)
    if not config_path(root, label).is_file():
        sys.exit(f"no campaign {label!r} here (looked for {config_path(root, label)})")

    operated_at = operated_level(root, label)

    venv = venv_path(operated_at, label).resolve()
    prefix = Path(sys.prefix).resolve()
    if prefix != venv:
        sys.exit(
            f"not running in the venv of campaign {label!r}\n"
            f"  expected: {venv}\n"
            f"  running:  {prefix}\n"
            f"Source the campaign's env.sh:\n"
            f"  source {env_path(operated_at, label)}"
        )

    lock = uv_lock_path(operated_at, label)
    if not lock.is_file():
        sys.exit(f"campaign {label!r} has no {UV_LOCK_FILENAME} ({lock})")
    ok, detail = venv_matches_lock(campaign_dir(operated_at, label))
    if not ok:
        sys.exit(
            f"the venv of campaign {label!r} does not match its committed "
            f"{UV_LOCK_FILENAME}\n"
            "The lock and the environment have drifted — the lock was bumped and "
            "the venv not rebuilt, or the venv was changed by hand.\n"
            f"Converge it:  mechababs campaign update-env\n"
            f"  lock:  {lock}\n"
            f"  venv:  {venv}" + _quoted(detail)
        )
    return campaign


def _member_selector(study, label):
    """How ``--study`` would name this member, for a message that has to be typable.

    The selector is a path **relative to the superstudy** (``resolve_member``), which
    is not the member's bare name when a superstudy nests its members. Falls back to
    the name when the super cannot be located — the hint is then still the right
    shape, and a hint is all it is.
    """
    above = superstudy_of(study, label)
    if above:
        return Path(study).resolve().relative_to(above).as_posix()
    return Path(study).name


def require_study_lock_match(study, label):
    """The inner verbs' one environment check: this venv vs **this study's** lock.

    Not ``require_env_match``. An inner verb has a second entry path that never
    passes through an outer command — ``datalad rerun`` executes the recorded
    command directly — so it must validate its own environment wherever it is
    replayed, and it must do so against the study it is operating in. Hence no
    location check: which *directory* the venv sits in is a selection question, and
    selection belongs to the outer commands. Two campaigns with identical locks are
    provenance-identical tools, so the lock distinguishes every case that matters.

    One check, serving three:

    - **reproduction.** A re-runner builds any venv from the study's committed lock
      and reruns; the check passes, and attribution is honest by construction. The
      member footprint is a complete uv project (``pyproject.toml`` *and*
      ``uv.lock`` are copied down), so this works with nothing from the superstudy.
    - **replay at the right epoch.** Rerun at the run's own commit and the lock in
      force there is the one checked, so the answer is the epoch's, not today's.
    - **the member-drift gate.** On the dispatched path the outer guard has already
      proved venv = *canonical* lock, so this failing means exactly one thing: the
      member's copy is behind. A drifted member is refused, never auto-refreshed —
      moving its remaining work onto new tools is a human acknowledgment, which is
      what ``campaign update-env --study`` records.
    """
    campaign = campaign_dir(study, label)
    lock = uv_lock_path(study, label)
    if not lock.is_file():
        sys.exit(
            f"campaign {label!r} in this study has no {UV_LOCK_FILENAME} ({lock})\n"
            "Every study carries the lock of the campaign that works in it — it is "
            "the study's own record of which tools ran, and what a rerun rebuilds "
            "from."
        )
    ok, detail = venv_matches_lock(campaign)
    if ok:
        return campaign

    message = (
        f"the running environment does not match the {UV_LOCK_FILENAME} committed "
        f"in this study for campaign {label!r}\n"
        f"  lock:    {lock}\n"
        f"  running: {sys.prefix}\n"
    )
    owner = recorded_superstudy_id(study, label)
    if owner:
        # A member: the venv doing the work is the superstudy's, so the mismatch is
        # this member's lock copy lagging the canonical one, not a broken venv.
        # Refreshing it is a deliberate act, taken at the level that owns the
        # environment -- never a side effect of the iterate that noticed.
        message += (
            "\nThis study is a member of a superstudy campaign, so the environment "
            "advancing it is the superstudy's and this lock copy is behind it. "
            "Its remaining work would then be recorded against tools its own "
            "history does not name.\n"
            "Move it onto the current environment, at the superstudy:\n"
            f"  mechababs campaign update-env --study {_member_selector(study, label)}"
        )
    else:
        message += (
            "\nRebuild the environment this study's lock describes, or activate "
            "the one that matches it:\n"
            f"  uv sync --frozen --project {campaign}"
        )
    sys.exit(message + _quoted(detail))


class Selected(NamedTuple):
    """What an operating verb has established about where it is standing.

    ``root`` is where the verb stands and the work happens; ``operated_at`` is the
    level the campaign was configured at, which is the same directory unless
    ``root`` is a member of a super-campaign. Both are carried so no call site
    derives one from the other — see ``operated_level``.
    """

    root: Path
    label: str
    campaign_dir: Path
    operated_at: Path


def require_campaign_level(path="."):
    """Where you are standing and which campaign — everything but the env guard.

    Returns ``(root, label)``. The split exists for exactly one command:
    ``campaign update-env`` must run when the venv is absent (a fresh clone) or
    stale (the guard just refused), which is the moment the guard cannot be
    satisfied — so it takes the configured-level and selection context and skips the
    environment check, the way ``campaign init`` does.

    It is **not** a relaxation of the configured-level rule: a member still refuses
    here, so ``update-env`` is an outer command like the rest and reaches a member
    only through ``--study``, from the superstudy.
    """
    root = study_mod.require_study_root(path)
    label = selected_label()
    # Asked of the marker's PRESENCE, not of whether the super can be found, and
    # with no override: a member cloned on its own would otherwise advance cells
    # that the super's catalog never hears about. A detached member supports
    # reproduction (`datalad rerun`, which carries its own environment check), not
    # advancing.
    owner = recorded_superstudy_id(root, label)
    if owner:
        where = superstudy_of(root, label)
        sys.exit(
            f"campaign {label!r} is operated from its superstudy, not from here.\n"
            f"  superstudy: {where if where else f'not found here (datalad-id {owner})'}\n"
            f"A campaign is operated only from the level it was configured at, so "
            f"this member carries no environment of its own."
        )
    return root, label


def require_selected_campaign(path="."):
    """The preconditions every *operating* verb shares, in one call.

    At a study root (``require_study_root``), with a campaign selected
    (``selected_label``), operated from the level it was configured at, and
    running the environment that campaign records (``require_env_match``).
    Returns a ``Selected``.

    The level check comes **before** the env-match guard on purpose: a member of a
    super-campaign has no venv of its own, so the env guard reached first would say
    "source the campaign's env.sh" naming a file that will never exist there.

    ``campaign init`` is the one command that does not take this: it runs before
    the environment exists — it is what creates it. ``campaign update-env`` takes
    the level half alone (``require_campaign_level``), for the same reason.
    """
    root, label = require_campaign_level(path)
    return Selected(
        Path(root), label, require_env_match(root, label), operated_level(root, label)
    )
