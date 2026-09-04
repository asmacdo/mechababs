"""update_env.py — the body of ``mechababs campaign update-env``.

Converges the environment on the declaration. It is two uv commands and a plain
save, and that thinness is the point: the campaign's ``pyproject.toml`` is the
declaration of intent, ``uv.lock`` is the recorded resolution, and mechababs owns
neither — it only makes sure the two agree and that both are committed.

**The behavior follows from the declaration's state**, so there is one verb rather
than a rebuild flag and a bump flag:

- pyproject untouched → ``uv lock`` moves nothing (a branch pin already locked is
  *not* chased to its tip by a bare re-lock) → this is rebuild-from-lock: a fresh
  clone, a wiped site, a passive ``git pull``, a historical checkout during
  rerun-reproduction;
- pyproject edited → the changed declaration re-resolves and installs. That is the
  deliberate mid-campaign bump, and the answer to the env-match guard's "now what".

**Bumping is a hand-edit to the pyproject**, not a flag. Rewrite-the-TOML flags
would make mechababs a package manager over the user's own declaration, and could
not cover the non-pin edits (a datalad floor, a constraint, an extra) that a
hand-edit covers for free. Every other config in mechababs is edited by hand; the
pyproject is the same kind of object, and ``campaign init`` generates it with the
source lines already in place, so a bump is one word on an existing line.

The exception is ``--upgrade <pkg>``, which exists because it is the one case with
nothing to edit: a pin that says "track this branch" whose branch moved. The
pyproject already declares the intent, so re-stating it as a sha would collapse
declaration into resolution and erase which branch the campaign follows. It passes
``uv lock --upgrade-package``, touching only the lock.

**A plain save, never a ``datalad run``.** ``uv lock`` resolves against the live
world — today's PyPI, today's branch tips — so recording it as a re-executable
command would promise a reproduction it cannot deliver. The lock file is the
reproducible artifact; the command that produced it is not.
"""

import shutil
import sys
from pathlib import Path

import yaml

from mechababs import campaign as campaign_mod
from mechababs import campaign_init, utils


def uv_for_update(prefix=None):
    """Which ``uv`` runs the update: this venv's if there is one, else PATH's.

    The one outer command that cannot insist on ``campaign.uv_bin()``. Its whole job
    is to run when the environment is absent or wrong — a fresh clone with a lock and
    no venv, a venv the guard just refused — so demanding the campaign's own uv would
    make it unavailable exactly when it is needed. When this process *is* a built
    campaign venv its uv is the pinned one and is preferred; otherwise fall back to
    the PATH uv that ``campaign init`` itself resolves.
    """
    candidate = Path(prefix or sys.prefix) / "bin" / "uv"
    return str(candidate) if candidate.is_file() else campaign_init.UV


def staged_cluster(root, label):
    """The campaign's own copy of the cluster config, for a build failure to name.

    The staged copy rather than whatever path the user originally passed: that copy
    is what is committed with the campaign, so it is the file to edit, and it reads
    the same whether the config arrived as a path or a URL.
    """
    campaign = campaign_mod.campaign_dir(root, label)
    config = yaml.safe_load(campaign_mod.config_path(root, label).read_text()) or {}
    cluster = config.get("cluster")
    return campaign / cluster if cluster else campaign


def resolve_member(superstudy, arg, label):
    """``--study`` to a member that already carries this campaign's footprint.

    Deliberately narrower than ``add-dataset``'s resolver, which clones a URL in:
    there is nothing to bring in here. A member without a footprint has no lock copy
    to refresh, and creating one would be selecting it into the campaign — which is
    ``add-dataset``'s decision, not an environment update's side effect.
    """
    superstudy = Path(superstudy).resolve()
    path = Path(arg)
    member = (path if path.is_absolute() else superstudy / path).resolve()
    if not member.is_relative_to(superstudy):
        sys.exit(
            f"{member} is not inside this superstudy ({superstudy}).\n"
            f"--study names a member of the superstudy you are standing in."
        )
    if not campaign_mod.config_path(member, label).is_file():
        sys.exit(
            f"{member} carries no campaign {label!r}, so it has no lock copy to "
            f"refresh.\nA member receives the campaign when `mechababs add-dataset "
            f"--study {arg}` first selects a source dataset in it."
        )
    return member


def _relative(root, *paths):
    return [str(Path(p).resolve().relative_to(Path(root).resolve())) for p in paths]


def save_declaration(root, label, message):
    """Commit the pyproject and the lock at ``root``, if either moved.

    No clean-in check, and that is deliberate rather than an omission: the documented
    way to bump a campaign is to **edit the pyproject by hand** and run this, so the
    declaration is dirty by design when it arrives here. Committing the edit together
    with the resolution it produced is the point — the two belong in one commit, and
    a guard demanding a clean tree would refuse the command's primary use.

    Scoped to those two files, so nothing else a user has in flight is swept in.
    """
    paths = _relative(
        root,
        campaign_mod.pyproject_path(root, label),
        campaign_mod.uv_lock_path(root, label),
    )
    if not utils.shallow_status(root, *paths):
        print(
            "the declaration and the lock are unchanged — nothing to commit "
            "(the venv was rebuilt from the lock as it stands)",
            file=sys.stderr,
        )
        return False
    utils.save_paths(root, paths, message)
    return True


def copy_lock_to_member(superstudy, member, label):
    """Give ``member`` the campaign's current lock, as its own committed act.

    **The lock only** — never the configs. A member's footprint is the member's own
    after creation, and may carry deliberate per-study config overrides, so nothing
    about a member is auto-synced; how a canonical *config* edit propagates is a
    separate, undecided question.

    No uv runs here. There is one venv, at the configured level, so the copy is a
    record rather than a control: the study's own claim about which tools its
    remaining work runs under, which is what the inner verbs check.

    Every level stays clean, nested outer-first as in ``add-dataset``: the member
    commits its lock, then the superstudy commits the gitlink that points at it.
    """
    canonical = campaign_mod.uv_lock_path(superstudy, label)
    copy = campaign_mod.uv_lock_path(member, label)
    member_rel = (
        Path(member).resolve().relative_to(Path(superstudy).resolve()).as_posix()
    )

    with utils.campaign_save_scope(superstudy, member) as super_save:
        with utils.campaign_save_scope(member, copy) as member_save:
            shutil.copy2(canonical, copy)
            member_save.message = (
                f"mechababs campaign update-env --study {member_rel} "
                f"(campaign {label!r}: this study's work runs at this lock from here on)"
            )
        super_save.message = (
            f"mechababs campaign update-env --study {member_rel} (campaign {label!r})"
        )
    return copy


def run_update_env(root=".", *, upgrade=(), member=None):
    """Re-resolve, install, and commit the selected campaign's environment.

    Exempt from the env-match guard the way ``campaign init`` is — it runs exactly
    when that guard fails or the venv is absent — but an outer command in every other
    respect: ``require_campaign_level`` still refuses at a member, so a member is
    reached with ``--study``, from the superstudy that owns the environment.
    """
    root, label = campaign_mod.require_campaign_level(root)
    campaign = campaign_mod.campaign_dir(root, label)
    if not campaign_mod.config_path(root, label).is_file():
        sys.exit(
            f"no campaign {label!r} here (looked for "
            f"{campaign_mod.config_path(root, label)})"
        )

    if member and not campaign_mod.is_superstudy_campaign(root, label):
        sys.exit(
            f"campaign {label!r} here is configured at a study, so there is no "
            f"member to name.\n--study refreshes one member's lock copy at a "
            f"superstudy; drop it to update this campaign's own environment."
        )
    target = resolve_member(root, member, label) if member else None

    uv = uv_for_update()
    cluster_file = staged_cluster(root, label)
    upgrade = list(upgrade)

    # The single writer, spanning the resolve, the install, the save and the
    # member's copy: this rewrites the uv.lock `iterate` dispatches work against, so
    # an iterate must not read it mid-rewrite and two update-envs must not resolve
    # into it at once.
    with utils.flocked(campaign_mod.flock_path(root)):
        _converge(root, label, campaign, upgrade, uv, cluster_file)
        if target:
            copy_lock_to_member(root, target, label)
    return 0


def _converge(root, label, campaign, upgrade, uv, cluster_file):
    """Re-resolve, install, and commit — the bare update, under the caller's lock."""
    lock_args = ["lock", "--project", str(campaign)]
    for package in upgrade:
        # A pure passthrough: uv decides what "newest satisfying the declaration"
        # means per source kind (the tip for a branch pin, a no-op for a sha).
        lock_args += ["--upgrade-package", package]

    # The same missing-wheel diagnosis as init, with update-env's way back (see
    # campaign_init.UPDATE_ENV_RETRY).
    uv_kwargs = dict(
        campaign=campaign,
        cluster_file=cluster_file,
        uv=uv,
        retry=campaign_init.UPDATE_ENV_RETRY,
    )
    campaign_init.run_uv(*lock_args, **uv_kwargs)
    campaign_init.run_uv("sync", "--project", str(campaign), "--frozen", **uv_kwargs)

    flags = "".join(f" --upgrade {p}" for p in upgrade)
    save_declaration(
        root, label, f"mechababs campaign update-env{flags} (campaign {label!r})"
    )
