"""add_dataset.py — the body of ``mechababs add-dataset --sourcedata <path>``.

Selecting which source datasets a campaign acts on is a separate, explicit step from
creating the campaign: ``campaign init`` fixes the app bundle, the cluster and the
environment; ``add-dataset`` says *on what*. A study may hold several source datasets
and a campaign may act on only some, so the selection cannot be implied by either.

**It does not bring data in.** The source dataset must already be in the study —
cloned there, or authored there by another tool. add-dataset selects it; it never
installs, fetches, or creates one.

Two acts, and only these two:

**Sniff** — verify the sourcedata is where the user says, and read the study's
per-subject metadata for it to fill the cell's identity columns (``processing_level``,
``n_subjects``, ``n_sessions``). These are *inputs*: the reconciler reads them and
never overwrites them.

**Add-state-entry** — append one row per (this source dataset x each app in the
campaign's bundle) to the study's statefile shard, with ``depends_on`` from each app
config and the derived columns empty. Empty ``babs`` is what makes the next
``iterate`` scaffold the cell.

The subject *inclusion* is deliberately not generated here. It is written at scaffold
time, where the pipeline's eligibility rule and the campaign's ``--limit`` apply, and
pinned inside that cell's ``datalad run``.
"""

import os
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

import yaml
from datalad.api import Dataset

from mechababs import campaign as campaign_mod
from mechababs import campaign_init, select, utils
from mechababs import study as study_mod


def resolve_sourcedata(study, sourcedata):
    """``--sourcedata`` as a study-relative path; refuse anything outside or absent.

    add-dataset runs from the campaign root — the study — like every operating verb,
    so the path is taken relative to it (an absolute path inside the study is fine
    too). There is no locator machinery: where you stand *is* the study.
    """
    study = Path(study)
    path = Path(sourcedata)
    src = (path if path.is_absolute() else study / path).resolve()
    if not src.is_relative_to(study):
        sys.exit(
            f"{src} is not inside this study ({study}).\n"
            f"add-dataset runs from the study root and selects data inside it."
        )
    if not src.exists():
        sys.exit(
            f"no such sourcedata: {src}\n"
            f"add-dataset selects a source dataset that is already in the study "
            f"— it does not install one."
        )
    if not src.is_dir():
        sys.exit(
            f"not a directory: {src}\n"
            f"--sourcedata names a source dataset's directory (e.g. "
            f"sourcedata/ds000001), not a file inside it."
        )
    return src.relative_to(study).as_posix()


def campaign_apps(study, label):
    """The campaign's ordered app bundle as ``[(app_config, depends_on), …]``.

    ``app_config`` — the cell's identity — is the **campaign-relative config path**
    (``bids-app-configs/MRIQC-24.0.2.yaml``), exactly as ``campaign.yaml`` lists it;
    the filename stem is only a derived, human-facing form. ``depends_on`` is
    *declared* in an app config by the producer's stem (the natural name to write)
    and resolved here to the producer's path, so both statefile identity columns
    hold the same kind of thing. An undeclarable stem stays unresolved and fails
    the dependency check below with the declared text in the message.

    Read from the campaign's own copies of the configs, not from wherever the user
    originally kept them: the copy in the study is what the run reproduces from.
    """
    config = yaml.safe_load(campaign_mod.config_path(study, label).read_text()) or {}
    apps = config.get("apps") or []
    if not apps:
        sys.exit(
            f"campaign {label!r} has no apps in "
            f"{campaign_mod.CONFIG_FILENAME} — nothing to add a dataset to"
        )
    by_stem = {Path(rel).stem: rel for rel in apps}
    pairs = []
    for rel in apps:
        declared = campaign_init.declared_depends_on(
            campaign_mod.campaign_dir(study, label) / rel
        )
        pairs.append((rel, by_stem.get(declared, declared) if declared else ""))
    return pairs


def check_dependencies(source_dataset, apps):
    """Refuse a cell whose producer would not be in the shard once this add lands.

    ``depends_on`` is resolved as a **row lookup within one shard** — the same source
    dataset's upstream-app row — so a dependent cell with no producer row is an edge
    that can never resolve, and the reconciler would silently park the cell forever.
    Failing here, at the moment the cell is written, is the loud version.

    The bundle is fixed at ``campaign init`` and always added whole (bundle growth is
    deliberately unsupported — con/mechababs#116), so the producer can only be in this
    same batch; the check guards a hand-assembled ``campaign.yaml`` that init's own
    bundle check never saw.
    """
    present = {name for name, _ in apps}
    for name, upstream in apps:
        if upstream and upstream not in present:
            sys.exit(
                f"app {name!r} depends on {upstream!r}, which has no cell for "
                f"{source_dataset} in this campaign.\n"
                f"Add the producer first — a dependency is resolved within one "
                f"study's statefile, so the upstream cell has to be there."
            )


def looks_like_url(arg):
    """True for something ``datalad clone`` should be handed rather than a path.

    Deliberately crude: a scheme, or the ``host:path`` form ssh uses. A local
    path that happens to contain a colon is vanishingly rare next to the cost of
    guessing wrong in the other direction, where a URL read as a path produces a
    confusing "no such member" instead of a clone.
    """
    return bool(urllib.parse.urlparse(arg).scheme) or re.match(r"^[^/]+@[^/]+:", arg)


def resolve_member(superstudy, arg):
    """``--study`` to a member directory, cloning it in first if it is a URL.

    The one case where a selection verb brings something in, and a narrow one:
    a member study is the *container* for source data, not the data. Cloning it
    is what "add-dataset does not install data" is not about — the raw BIDS
    content underneath is still never fetched.
    """
    if looks_like_url(arg):
        name = Path(urllib.parse.urlparse(arg).path.rstrip("/")).name
        name = name[:-4] if name.endswith(".git") else name
        dest = Path(superstudy) / name
        if dest.exists():
            sys.exit(
                f"{dest} already exists; pass --study {name} to select into it "
                f"rather than a URL to clone it again."
            )
        Dataset(str(superstudy)).clone(
            source=arg, path=name, result_renderer="disabled"
        )
        return dest.resolve()

    path = Path(arg)
    member = (path if path.is_absolute() else Path(superstudy) / path).resolve()
    if not member.is_relative_to(Path(superstudy)):
        sys.exit(
            f"{member} is not inside this superstudy ({superstudy}).\n"
            f"--study names a member of the superstudy you are standing in."
        )
    if not study_mod.is_study_root(member):
        sys.exit(
            f"not a member study: {member}\n"
            f"--study names a study already in this superstudy, or a URL to "
            f"clone one in."
        )
    return member


def write_member_footprint(superstudy, member, label):
    """Give ``member`` this campaign's footprint, if it has not got one yet.

    A member receives the campaign at the moment it is first selected into it,
    which is why ``campaign init`` at a super fans out to nothing: no members are
    chosen yet. The copy is the config epoch made local — the same configs and the
    same lock — so the member reproduces its own derivatives from its own contents,
    without the superstudy.

    No ``env.sh`` and no venv: the operational environment lives at the configured
    level, and a member of a super-campaign is not operated from.
    """
    dest = campaign_mod.campaign_dir(member, label)
    if dest.exists():
        return dest
    src = campaign_mod.campaign_dir(superstudy, label)
    dest.mkdir(parents=True)
    for name in (
        ".gitattributes",
        ".gitignore",
        campaign_mod.PYPROJECT_FILENAME,
        campaign_mod.UV_LOCK_FILENAME,
    ):
        if (src / name).is_file():
            shutil.copy2(src / name, dest / name)
    for dirname in (campaign_mod.APPS_DIRNAME, campaign_mod.CLUSTERS_DIRNAME):
        if (src / dirname).is_dir():
            shutil.copytree(src / dirname, dest / dirname)

    # The config, plus the marker that says which level operates this campaign.
    config = yaml.safe_load((src / campaign_mod.CONFIG_FILENAME).read_text()) or {}
    config[campaign_mod.SUPERSTUDY_KEY] = os.path.relpath(superstudy, member)
    (dest / campaign_mod.CONFIG_FILENAME).write_text(
        yaml.safe_dump(config, sort_keys=False)
    )
    (dest / campaign_mod.STATE_FILENAME).write_text(campaign_mod.initial_header())
    return dest


def add(sourcedata, member_arg=None):
    """Select ``sourcedata`` into the selected campaign. Returns the rows added.

    Runs from the campaign root, like every operating verb. For a study-configured
    campaign that root is the study and ``sourcedata`` is a path inside it. For a
    super-configured one it is the superstudy, and reaching a member takes a second
    coordinate rather than a different place to stand: ``member_arg`` (``--study``)
    selects the member and ``sourcedata`` re-bases onto it.
    """
    root, label, _ = campaign_mod.require_selected_campaign()
    at_super = campaign_mod.is_superstudy_campaign(root, label)

    # Both directions of the configured-level rule, refused at the door.
    if member_arg and not at_super:
        sys.exit(
            f"campaign {label!r} here is configured at a study, so there is no "
            f"member to name.\n--study selects a member of a superstudy; drop it "
            f"and pass --sourcedata alone."
        )
    if at_super and not member_arg:
        sys.exit(
            f"campaign {label!r} here is configured at a superstudy, so a source "
            f"dataset lives in one of its members.\n"
            f"Name it: --study <member> --sourcedata <path inside that member>."
        )
    superstudy = root if at_super else None
    study = resolve_member(root, member_arg) if at_super else root
    if superstudy:
        write_member_footprint(superstudy, study, label)
    source_dataset = resolve_sourcedata(study, sourcedata)

    apps = campaign_apps(study, label)
    try:
        identity = select.sniff_source_dataset(study, Path(source_dataset).name)
    except RuntimeError as e:
        # The metadata TSV is the study input mechababs expects to be there
        # (docs/spec.md, "Layout & input"); generating one when absent is follow-up
        # generalization work, so say what is missing rather than guessing at counts.
        sys.exit(f"cannot read the study metadata for {source_dataset}: {e}")

    # Flock first (the campaign's single-writer guarantee), clean-in second (the
    # statefile must be untouched before this write, so the commit is attributable),
    # then the read-modify-write — committed as one node when the scope exits.
    with (
        utils.flocked(campaign_mod.flock_path(study, label)),
        utils.campaign_save_scope(study, campaign_mod.state_path(study, label)) as save,
    ):
        rows = campaign_mod.read_state(study, label)
        # The bundle is fixed at init, so a dataset is selected whole or not at all —
        # re-adding refuses. To run more apps on this data, start a new campaign
        # (a new config epoch): bundle growth is deliberately unsupported (#116).
        if any(r["source_dataset"] == source_dataset for r in rows):
            sys.exit(
                f"{source_dataset} is already selected into campaign {label!r}.\n"
                f"The app bundle is fixed at campaign init — to run more apps "
                f"on this data, create a new campaign."
            )
        check_dependencies(source_dataset, apps)

        added = [
            {
                "source_dataset": source_dataset,
                "app_config": name,
                "depends_on": upstream,
                **identity,
            }
            for name, upstream in apps
        ]
        campaign_mod.write_state(study, label, rows + added)
        save.message = (
            f"mechababs add-dataset {source_dataset} "
            f"({identity['processing_level']}-level; "
            f"{', '.join(row['app_config'] for row in added)})"
        )

    # The superstudy's own write, and deliberately a second commit rather than one
    # spanning both: the member's cells and the super's membership live in different
    # datasets, so each records its own change where a reader of that dataset alone
    # will find it. The member is saved first, so the gitlink the super registers
    # already points at the member state this catalog row describes.
    if superstudy:
        member_rel = Path(study).relative_to(Path(superstudy)).as_posix()
        with (
            utils.flocked(campaign_mod.flock_path(superstudy, label)),
            utils.campaign_save_scope(
                superstudy, campaign_mod.campaign_dir(superstudy, label)
            ) as save,
        ):
            members = campaign_mod.read_members(superstudy, label)
            members.append(
                {
                    "study": member_rel,
                    "source_dataset": source_dataset,
                    "lifecycle": campaign_mod.LIFECYCLE_PENDING,
                }
            )
            campaign_mod.write_members(superstudy, label, members)
            save.message = (
                f"mechababs add-dataset {member_rel}/{source_dataset} "
                f"(member selected into campaign {label!r})"
            )
    return added
