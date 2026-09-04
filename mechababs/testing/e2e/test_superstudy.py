"""The superstudy layer: one campaign at a superstudy, fanned out over its members.

The spine (`test_spine.py`) proves a campaign configured **at a study**.
This proves the other level: a campaign configured at a **superstudy**, where the
campaign root and the place the work happens are no longer the same directory. That
split is the whole subject of this module, and it is where the layer's bugs live —
everything that "just works" at a study because the two coincide has to be checked
again here, once, against a real filesystem.

**Why an end-to-end test and not more unit tests.** The layer's characteristic bug
is resolving the campaign environment at the *member*, which by construction has
no venv, so no superstudy transition can scaffold at all. A unit test of the guard
is written against the same assumption the guard makes; only a real fan-out,
dispatching a real inner verb with a member as cwd, asks the question the right way
round. `_stage_fanout_scaffold` is that question.

**Stages over one superstudy, like `test_spine`.** `campaign init` builds a venv, which
is the slowest thing here, so the stages share one campaign rather than paying for it
per test. Later stages are meaningless if earlier ones failed, so a single test keeps a
single cause from painting the whole file red.

**Two rungs, split at submit**, exactly as in `test_spine`: everything up to scaffold is
`babs init` and git and runs on a developer's host; from submit on, real jobs need a
real scheduler and are skipped-with-a-reason where there is none.
"""

import csv
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from conftest import BUMP_PACKAGE, bump_declaration
from mechababs import babs_status
from mechababs import campaign as campaign_mod

log = logging.getLogger("mechababs.e2e")

LABEL = "e2e-super"
ADOPTED_LABEL = "e2e-adopted"

JOB_WAIT_SECONDS = 900
JOB_POLL_SECONDS = 10

ANCHOR = "SimBIDS-0.0.3+anchor"
CHAIN = "SimBIDS-0.0.3+chain"

DATASET_ID = "ds999999"
SOURCEDATA = f"sourcedata/{DATASET_ID}"

# The member the fixture study is cloned in as. It is also what `--study` names, and
# what the catalog records, so it is spelled once here.
MEMBER = f"study-{DATASET_ID}"
MEMBER_2 = f"study-{DATASET_ID}-second"


# --------------------------------------------------------------------------
# Driving the CLI
#
# Deliberately duplicated from test_spine rather than imported: the e2e directory is
# not a package, so a cross-module import would rely on pytest's sys.path insertion
# and break under a different import mode. These are a dozen lines; the coupling is
# not worth the fragility.
# --------------------------------------------------------------------------


def _run(cmd, cwd, *, env=None, check=True):
    log.info("$ %s   (in %s)", " ".join(str(c) for c in cmd), cwd)
    proc = subprocess.run(cmd, cwd=str(cwd), env=env, text=True, capture_output=True)
    if proc.stdout:
        log.info("stdout:\n%s", proc.stdout)
    if proc.stderr:
        log.info("stderr:\n%s", proc.stderr)
    if check:
        assert proc.returncode == 0, (
            f"{cmd[0]} failed ({proc.returncode}):\n{proc.stderr}"
        )
    return proc


def _driver_mechababs():
    """The `mechababs` running this scenario — the one that creates the campaign."""
    exe = Path(sys.executable).parent / "mechababs"
    assert exe.is_file(), (
        f"no `mechababs` beside {sys.executable} — the code under test is not "
        "installed in the environment running this suite"
    )
    return str(exe)


def _at_super(superstudy, *args, label=LABEL, check=True):
    """Run an operating verb the way a user does at a superstudy: source, then run.

    The superstudy IS the campaign root here, so this is the same `source env.sh &&
    mechababs …` a user types — the point being that there is only ever one place to
    stand, and it is not the member.
    """
    env_sh = campaign_mod.env_path(superstudy, label)
    script = f'. "{env_sh}" && mechababs ' + " ".join(f'"{a}"' for a in args)
    return _run(["bash", "-c", script], superstudy, check=check)


def _at_member(superstudy, member, *args, check=True):
    """Run a verb standing in the MEMBER, with the superstudy's environment active.

    Not something a user is meant to do — it is what the configured-level rule
    forbids — so this exists to prove the refusal is real rather than conventional.
    The environment is sourced from the superstudy because the member has none, which
    is exactly the asymmetry under test.
    """
    env_sh = campaign_mod.env_path(superstudy, LABEL)
    script = f'. "{env_sh}" && mechababs ' + " ".join(f'"{a}"' for a in args)
    return _run(["bash", "-c", script], superstudy / member, check=check)


def _babs_status(superstudy, project):
    env_sh = campaign_mod.env_path(superstudy, LABEL)
    proc = _run(
        [
            "bash",
            "-c",
            f'. "{env_sh}" && babs status --json "$1"',
            "e2e",
            str(project),
        ],
        superstudy,
    )
    return json.loads(proc.stdout)


def _wait_for_jobs(superstudy, project):
    """Poll the same decision seam the reconciler routes on, bounded."""
    deadline = time.monotonic() + JOB_WAIT_SECONDS
    while True:
        status = _babs_status(superstudy, project)
        action = babs_status.decide(status)
        log.info("babs status: %s -> %s", status, action)
        if action != "skip":
            return status
        assert time.monotonic() < deadline, (
            f"jobs still in flight after {JOB_WAIT_SECONDS}s: {status}"
        )
        time.sleep(JOB_POLL_SECONDS)


def _skip_without_scheduler(stage):
    if shutil.which("sbatch"):
        return False
    log.warning(
        "SKIPPING %s: no `sbatch` on PATH, so no jobs can run here. These stages "
        "are the podman rung (mechababs/testing/e2e/run_in_podman.sh) or a real "
        "cluster; the stages above are host-runnable and just passed.",
        stage,
    )
    return True


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, text=True, capture_output=True
    ).stdout


def _assert_clean(path, phase, *, what="tree"):
    assert not _git(path, "status", "--porcelain").strip(), (
        f"{what} at {path} dirty after {phase} — mechababs left work uncommitted:\n"
        + _git(path, "status", "--porcelain")
    )


def _assert_every_level_clean(superstudy, phase, *members):
    """The every-level-clean rule, asserted as such: no level is left carrying the next one's work.

    Checked bottom-up so the failure names the deepest dirty level, which is the one
    that caused it — a dirty member shows at the super as a moved gitlink, and
    reporting that first would point at the wrong place.
    """
    for member in members:
        _assert_clean(superstudy / member, phase, what=f"member {member}")
    _assert_clean(superstudy, phase, what="superstudy")


def _state_rows(study, label=LABEL):
    with open(campaign_mod.state_path(study, label), newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def _member_rows(superstudy, label=LABEL):
    with open(campaign_mod.members_path(superstudy, label), newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def _run_record(study):
    body = _git(study, "log", "-1", "--format=%b")
    return json.loads(body[body.index("{") : body.rindex("}") + 1])


# --------------------------------------------------------------------------
# Fixture wiring specific to this layer
# --------------------------------------------------------------------------


def _localized_app_configs(app_configs, dest, containers):
    """Copy the suite's app configs, rewriting the container source to an abspath.

    The shipped configs name `../containers`, which `resolve_container_ds` resolves
    against the **study root** — correct for `test_spine`, whose studies are siblings
    of the container clone under the workdir. A member of a superstudy sits one level
    deeper, so the same relative path would resolve to `<superstudy>/containers` and
    miss.

    Rewriting the one field is deliberately preferred over shipping a second pair of
    configs differing by a `../`: the depth is a property of *where this scenario puts
    the study*, not of the app, and a duplicated config would drift from the one
    `test_spine` exercises. An absolute source is a supported value (a production
    config typically names a URL, the other absolute form).
    """
    dest.mkdir(parents=True, exist_ok=True)
    for name in (ANCHOR, CHAIN):
        config = yaml.safe_load((app_configs / f"{name}.yaml").read_text())
        config["mechababs"]["container"]["source"] = str(containers)
        (dest / f"{name}.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    return dest


def _clone_member(superstudy, template, name):
    """Clone the fixture study into the superstudy as a member, and leave it clean.

    A member arrives by being cloned in — either by hand, as here, or by
    `add-dataset --study <url>`. Copying one in instead would leave the superstudy
    with an untracked directory, which `add-dataset`'s clean-in would (rightly)
    refuse; `datalad clone -d` registers and saves the registration, which is the
    state a real superstudy is in before anything is selected.
    """
    _run(
        ["datalad", "clone", "-d", str(superstudy), str(template), name],
        cwd=superstudy,
    )
    # Belt-check rather than belief: whether `clone -d` saves the registration is
    # datalad's business, and this scenario's later clean-in assertions are only
    # meaningful if the superstudy really is clean going in.
    _run(
        ["datalad", "save", "-d", str(superstudy), "-m", f"register {name}", name],
        cwd=superstudy,
        check=False,
    )
    _assert_clean(superstudy, f"cloning the member {name}", what="superstudy")
    return superstudy / name


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------


def _stage_superstudy_init(superstudy, cluster_config, apps, mechababs_pin, babs_pin):
    """`campaign init --superstudy NAME` creates the superstudy and its campaign.

    The one dataset mechababs creates. A superstudy holds no data of its own, so
    there is nothing to fabricate — unlike a study, which mechababs never authors.

    The single structural difference from a study campaign is asserted here: the
    campaign dir carries a **membership catalog** and no statefile. State is never
    materialized at the super; its view of the campaign is computed from the members.
    """
    assert not superstudy.exists(), "the create path needs a name that is not there yet"

    _run(
        [
            _driver_mechababs(),
            "campaign",
            "init",
            LABEL,
            "--superstudy",
            superstudy.name,
            "--apps",
            f"{apps / f'{ANCHOR}.yaml'},{apps / f'{CHAIN}.yaml'}",
            "--cluster",
            str(cluster_config),
            *(["--mechababs", mechababs_pin] if mechababs_pin else []),
            *(["--babs", babs_pin] if babs_pin else []),
            "--limit",
            "1",
        ],
        cwd=superstudy.parent,
    )

    assert (superstudy / ".datalad").is_dir(), (
        "campaign init --superstudy did not create the superstudy as a datalad dataset"
    )

    # A catalog, not a statefile. This is the layout fact `is_superstudy_campaign`
    # reads, so getting it wrong would mis-route every verb.
    assert campaign_mod.members_path(superstudy, LABEL).is_file(), (
        "the superstudy campaign has no membership catalog"
    )
    assert not campaign_mod.state_path(superstudy, LABEL).is_file(), (
        "a superstudy campaign materialized a statefile; cell state lives in members"
    )
    assert _member_rows(superstudy) == [], "campaign init selected members"

    # The environment lives here, and only here — the asymmetry the whole layer
    # rests on, and the one the env-match guard had not been taught.
    assert (
        campaign_mod.venv_path(superstudy, LABEL) / "bin" / "mechababs"
    ).is_file(), "the superstudy campaign venv has no mechababs — uv sync did not run"
    assert campaign_mod.env_path(superstudy, LABEL).is_file(), (
        "the superstudy campaign has no env.sh to source"
    )

    _assert_clean(superstudy, "campaign init --superstudy", what="superstudy")


def _stage_add_dataset_needs_a_member(superstudy):
    """At a superstudy, a source dataset lives in a member — so naming one is required.

    Both directions of the configured-level rule are refused at the door; this is the
    half that only exists at a super, and it is a message rather than a traceback
    because it is a thing a user does, not a bug.
    """
    refused = _at_super(
        superstudy, "add-dataset", "--sourcedata", SOURCEDATA, check=False
    )
    assert refused.returncode != 0, (
        "add-dataset at a superstudy accepted a bare --sourcedata, with no member"
    )
    assert "--study <member>" in refused.stderr, refused.stderr


def _stage_add_dataset_selects_a_member(superstudy):
    """`add-dataset --study <member>` writes the footprint down and the row up.

    One selection, two levels, and the assertions are about who owns what: the member
    receives the campaign (its configs, its lock, its cells) and the superstudy
    records that it did (the catalog row plus the advanced gitlink). Every level is
    clean afterwards — nothing is left for a later commit to sweep up.
    """
    added = _at_super(
        superstudy, "add-dataset", "--study", MEMBER, "--sourcedata", SOURCEDATA
    )
    assert "2 cell(s)" in added.stderr, added.stderr

    member = superstudy / MEMBER

    # --- what the member received: the configs and the lock, made local -------
    member_campaign = campaign_mod.campaign_dir(member, LABEL)
    assert member_campaign.is_dir(), "the member got no campaign footprint"
    for name in (ANCHOR, CHAIN):
        assert (campaign_mod.apps_dir(member, LABEL) / f"{name}.yaml").is_file(), (
            f"the member's footprint is missing the {name} config"
        )
    assert campaign_mod.uv_lock_path(member, LABEL).is_file(), (
        "the member's footprint carries no lock — it cannot say what produced it"
    )

    # ...and, just as deliberately, what it did NOT receive. A member is not operated
    # from, so it has no environment of its own. This is the fact that made the
    # env-match guard fail: it is load-bearing, not incidental.
    assert not campaign_mod.venv_path(member, LABEL).exists(), (
        "the member got a venv; the operational environment lives at the super"
    )
    assert not campaign_mod.env_path(member, LABEL).exists(), (
        "the member got an env.sh; a member of a super-campaign is not operated from"
    )

    # The marker that says which level operates this campaign, and that it resolves.
    member_config = yaml.safe_load(campaign_mod.config_path(member, LABEL).read_text())
    assert campaign_mod.SUPERSTUDY_KEY in member_config, (
        "the member's campaign config does not record its superstudy"
    )
    assert campaign_mod.superstudy_of(member, LABEL) == superstudy.resolve(), (
        "the member's superstudy marker does not resolve back to the superstudy"
    )

    # --- the cells: in the member's shard, one per app ------------------------
    rows = _state_rows(member)
    assert len(rows) == 2, f"expected one cell per app in the member, got {rows}"
    assert [r["app_config"] for r in rows] == [
        f"{campaign_mod.APPS_DIRNAME}/{ANCHOR}.yaml",
        f"{campaign_mod.APPS_DIRNAME}/{CHAIN}.yaml",
    ], "the member's cells are not one per app, in bundle order"
    for row in rows:
        assert row["source_dataset"] == SOURCEDATA, row
        assert row["processing_level"] == "subject", row
        assert row["babs"] == "" and row["merged"] == "", row

    # --- the row up: membership, recorded at the level that coordinates -------
    members = _member_rows(superstudy)
    assert len(members) == 1, f"expected one catalog row, got {members}"
    assert members[0]["study"] == MEMBER, members[0]
    assert members[0]["source_dataset"] == SOURCEDATA, members[0]

    # --- one commit per level, member first -----------------------------------
    # The order matters and is asserted through its consequence: the superstudy's
    # commit registers a gitlink, so it can only be correct if the member had already
    # committed the footprint it points at. A clean superstudy is that proof.
    assert (
        _git(member, "log", "-1", "--format=%s")
        .strip()
        .startswith(f"mechababs add-dataset {SOURCEDATA}")
    ), _git(member, "log", "-1", "--format=%s")
    assert (
        _git(superstudy, "log", "-1", "--format=%s")
        .strip()
        .startswith(f"mechababs add-dataset {MEMBER}/{SOURCEDATA}")
    ), _git(superstudy, "log", "-1", "--format=%s")

    _assert_every_level_clean(superstudy, "add-dataset", MEMBER)


def _stage_a_member_is_not_operated_from(superstudy):
    """Standing in the member and running a verb is refused, and says why.

    The configured-level rule, checked where a user would actually trip over it. The
    refusal has to arrive *before* the env-match guard, or the message would tell them
    to source an `env.sh` that will never exist in a member — which is the confusing
    version of the same fact.
    """
    refused = _at_member(superstudy, MEMBER, "iterate", check=False)
    assert refused.returncode != 0, "a member campaign was operated from the member"
    assert "operated from its superstudy" in refused.stderr, refused.stderr
    assert "not running in the venv" not in refused.stderr, (
        "the env-match guard was reached first; the honest error is the level rule"
    )


def _stage_fanout_scaffold(superstudy):
    """The fan-out really scaffolds — the question this whole module exists to ask.

    `iterate` at the superstudy dispatches an inner verb with the **member** as cwd
    while the running interpreter is the **superstudy's** venv: the only
    configuration in which mechababs' two levels disagree about where the
    environment is (see the module docstring).

    Beyond that, this is scaffold's usual contract, checked one level up: the
    derivative in its final home inside the member, a run record in the member saying
    which command put it there, and the superstudy recording that the member moved.
    """
    run = _at_super(superstudy, "iterate", "--batch", "1")
    assert "superstudy iterate over 1 member(s)" in run.stderr, run.stderr

    member = superstudy / MEMBER
    derivative = f"derivatives/{ANCHOR}+{DATASET_ID}+{LABEL}"

    assert (member / derivative).is_dir(), (
        f"the fan-out did not scaffold a derivative at {derivative} — if the error "
        "mentions a venv, the env-match guard has regressed to resolving at the member"
    )
    rows = {r["app_config"]: r for r in _state_rows(member)}
    anchor = rows[f"{campaign_mod.APPS_DIRNAME}/{ANCHOR}.yaml"]
    assert anchor["babs"] == derivative, (
        f"the member's cell does not record the derivative: {anchor}"
    )

    # The transition is a run record in the MEMBER: that is where the work happened,
    # and the command in it is member-relative so it re-executes elsewhere.
    subject = _git(member, "log", "-1", "--format=%s").strip()
    assert subject.startswith("[DATALAD RUNCMD] mechababs scaffold"), subject
    record = _run_record(member)
    assert record["pwd"] == ".", record
    assert f"--campaign {LABEL}" in record["cmd"], record["cmd"]

    # ...and the superstudy records that the member advanced: the gitlink move, plus
    # the one piece of state the super commits. This is the member's first scaffold, so
    # its lifecycle leaves `registered` — and because it did, the subject is that rather
    # than what happened to the cells. The super coordinates; the member does the work.
    super_subject = _git(superstudy, "log", "-1", "--format=%s").strip()
    assert super_subject == (
        f"mechababs iterate: {MEMBER} {SOURCEDATA} is now "
        f"{campaign_mod.LIFECYCLE_ACTIVE} (campaign {LABEL!r})"
    ), super_subject
    catalog = campaign_mod.members_path(superstudy, LABEL).relative_to(superstudy)
    touched = _git(superstudy, "show", "--pretty=", "--name-only", "HEAD").split()
    assert sorted(touched) == sorted([MEMBER, str(catalog)]), (
        f"the superstudy's commit reaches past the member and its catalog: {touched}"
    )

    _assert_every_level_clean(superstudy, "the fan-out scaffold", MEMBER)


def _stage_fanout_submit_and_merge(superstudy):
    """`iterate` alone carries the cell to merged, and a real derivative lands in the member.

    The stage that proves the fan-out produces something rather than just moving
    bookkeeping. Nobody names a verb: the reconciler reads the member's shard, decides
    submit, then merge, and the operator only says "again".
    """
    if _skip_without_scheduler("_stage_fanout_submit_and_merge"):
        return

    member = superstudy / MEMBER
    project = member / "derivatives" / f"{ANCHOR}+{DATASET_ID}+{LABEL}"
    anchor_app = f"{campaign_mod.APPS_DIRNAME}/{ANCHOR}.yaml"

    # --- iterate: the cell is scaffolded and nothing is submitted -> submit ---
    head = _git(superstudy, "rev-parse", "HEAD").strip()
    _at_super(superstudy, "iterate", "--batch", "1")
    status = _babs_status(superstudy, project)
    assert status["total"] > 0, f"babs knows of no jobs to submit: {status}"
    assert status["submitted"] == status["total"], (
        f"the fan-out's submit left jobs undeployed: {status}"
    )
    # Submit changes nothing tracked, at either level — so the superstudy has nothing
    # to record, and must not invent a commit for a tick that moved no git state.
    assert _git(superstudy, "rev-parse", "HEAD").strip() == head, (
        "the superstudy committed for a submit, which changes nothing tracked"
    )
    _assert_every_level_clean(superstudy, "the fan-out submit", MEMBER)

    # --- wait, then iterate: all done -> merge --------------------------------
    final = _wait_for_jobs(superstudy, project)
    assert babs_status.decide(final) == "merge", (
        f"the jobs did not all succeed, so there is nothing to merge: {final}"
    )
    _at_super(superstudy, "iterate", "--batch", "1")

    rows = {r["app_config"]: r for r in _state_rows(member)}
    assert rows[anchor_app]["merged"] == "true", rows[anchor_app]

    # Content, not just a flag: the derivative carries committed per-subject results.
    tracked = _git(project, "ls-files").split()
    produced = [p for p in tracked if p.startswith("sub-") and p.endswith(".zip")]
    assert produced, (
        "the derivative carries no per-subject results after a fan-out merge:\n"
        f"{tracked}"
    )

    # The anchor merged but the chain has not, so the member's coarse lifecycle is
    # still `active` and the catalog is not rewritten to what it already says. With no
    # lifecycle change to lead with, the subject is the tick: the cell and what was
    # done to it.
    assert _git(superstudy, "log", "-1", "--format=%s").strip() == (
        f"mechababs iterate: {MEMBER} merged {SOURCEDATA} / {ANCHOR} "
        f"(campaign {LABEL!r})"
    ), _git(superstudy, "log", "-1", "--format=%s")
    assert _git(superstudy, "show", "--pretty=", "--name-only", "HEAD").split() == [
        MEMBER
    ], "the catalog was rewritten for a lifecycle that did not move"
    _assert_every_level_clean(superstudy, "the fan-out merge", MEMBER)


def _stage_a_second_member_and_narrowing(superstudy, study_template):
    """Two members: `--study` narrows the fan-out, and the catalog spends the budget.

    Membership is what the fan-out iterates, so a second member is the first time the
    catalog is doing any work. Two claims are checked, and they are the ones that make
    the catalog a priority interface rather than a list: `--study` advances one member
    and leaves the other exactly as it was, and `--batch` bounds the **whole iterate**
    rather than each member.
    """
    _clone_member(superstudy, study_template, MEMBER_2)
    _at_super(
        superstudy, "add-dataset", "--study", MEMBER_2, "--sourcedata", SOURCEDATA
    )

    members = [r["study"] for r in _member_rows(superstudy)]
    assert members == [MEMBER, MEMBER_2], (
        f"the catalog is not in selection order: {members}"
    )
    _assert_every_level_clean(superstudy, "selecting a second member", MEMBER, MEMBER_2)

    second = superstudy / MEMBER_2
    first_before = _git(superstudy / MEMBER, "rev-parse", "HEAD").strip()

    # --- narrowing: name the second member, and only it moves ------------------
    run = _at_super(superstudy, "iterate", "--study", MEMBER_2, "--batch", "1")
    assert "superstudy iterate over 1 member(s)" in run.stderr, run.stderr
    assert (second / "derivatives" / f"{ANCHOR}+{DATASET_ID}+{LABEL}").is_dir(), (
        "the narrowed iterate did not advance the member it named"
    )
    assert _git(superstudy / MEMBER, "rev-parse", "HEAD").strip() == first_before, (
        "an iterate narrowed to one member advanced the other"
    )

    # A member that was never selected is an error, not a silent no-op — a typo'd
    # --study must not report a successful iterate over nothing.
    refused = _at_super(superstudy, "iterate", "--study", "study-nope", check=False)
    assert refused.returncode != 0, "--study accepted a non-member"
    assert "is not a member" in refused.stderr, refused.stderr

    _assert_every_level_clean(superstudy, "the narrowed iterate", MEMBER, MEMBER_2)


def _stage_a_drifted_member_is_refused_until_acknowledged(superstudy):
    """The drift sequence, end to end: bump at the super, refuse, acknowledge, resume.

    The design's sharpest claim about members, and the one that is only true if three
    separate pieces line up. There is ONE venv, at the super, so a member cannot sit
    on an old environment — what a member *can* have is a lock copy that no longer
    describes the tools about to advance it. Advancing it in that state would write
    run records into a study whose own committed history names other tools, which is
    provenance falsification one level below where the outer guard can see it.

    So a drifted member is **refused, never auto-refreshed**: moving its remaining
    work onto a new environment is a human act, and `update-env --study` is the act.
    The refusal is the dispatched inner verb's (`require_study_lock_match`), not
    iterate's, which does no lock comparison at all.

    Needs a scheduler: the member's advanceable cell is the chain one, whose gate
    only opened because the anchor really merged.
    """
    if _skip_without_scheduler("_stage_a_drifted_member_is_refused_until_acknowledged"):
        return

    member = superstudy / MEMBER
    canonical = campaign_mod.uv_lock_path(superstudy, LABEL)
    copy = campaign_mod.uv_lock_path(member, LABEL)
    assert copy.read_text() == canonical.read_text(), (
        "the member is not at the canonical lock before the bump, so this stage "
        "would not be testing drift"
    )

    # --- the bump, at the super, where the environment lives -------------------
    bump_declaration(campaign_mod.campaign_dir(superstudy, LABEL))
    _at_super(superstudy, "campaign", "update-env")

    assert f'name = "{BUMP_PACKAGE}"' in canonical.read_text(), (
        "update-env did not re-resolve the canonical lock"
    )
    assert copy.read_text() != canonical.read_text(), (
        "the member's lock copy moved on its own — it is a record the member "
        "commits for itself, never something a super-level command syncs down"
    )
    _assert_every_level_clean(superstudy, "the canonical bump", MEMBER, MEMBER_2)

    # --- the refusal ------------------------------------------------------------
    member_before = _git(member, "rev-parse", "HEAD").strip()
    refused = _at_super(
        superstudy, "iterate", "--study", MEMBER, "--batch", "1", check=False
    )
    assert refused.returncode != 0, (
        "a member whose lock copy is behind the canonical one was advanced"
    )
    # The message is the interface here, so it is asserted as one: what is wrong, and
    # the exact command that fixes it. A user reads this and types the next line.
    assert "does not match the uv.lock committed in this study" in refused.stderr, (
        refused.stderr
    )
    assert f"mechababs campaign update-env --study {MEMBER}" in refused.stderr, (
        f"the refusal does not name the acknowledgment command:\n{refused.stderr}"
    )
    assert _git(member, "rev-parse", "HEAD").strip() == member_before, (
        "the refused iterate still advanced the member"
    )
    _assert_every_level_clean(superstudy, "the refused iterate", MEMBER, MEMBER_2)

    # --- the acknowledgment, typed exactly as the refusal printed it ------------
    super_before = _git(superstudy, "rev-parse", "HEAD").strip()
    _at_super(superstudy, "campaign", "update-env", "--study", MEMBER)

    assert copy.read_text() == canonical.read_text(), (
        "update-env --study did not bring the member onto the canonical lock"
    )
    # Every level stays clean, which means each committed its own half: the member
    # its lock copy, the super the gitlink pointing at that commit. A refresh that
    # left either for later would show up as a dirty level here.
    assert _git(member, "rev-parse", "HEAD").strip() != member_before, (
        "the member did not commit its refreshed lock copy"
    )
    assert _git(superstudy, "rev-parse", "HEAD").strip() != super_before, (
        "the superstudy did not commit the member's advanced gitlink"
    )
    assert f"update-env --study {MEMBER}" in _git(member, "log", "-1", "--format=%s"), (
        _git(member, "log", "-1", "--format=%s")
    )
    _assert_every_level_clean(superstudy, "the member's lock refresh", MEMBER, MEMBER_2)

    # The configs are the member's own, and a lock refresh is not a config sync — a
    # member may deliberately override them, so nothing but the lock moves. The
    # superstudy marker is the tell: it exists only in the member's copy, so a blind
    # copy-down of the canonical config would erase it and the member would stop
    # knowing whose campaign it carries.
    assert campaign_mod.recorded_superstudy_id(member, LABEL), (
        "the member's config lost its superstudy marker — update-env --study copied "
        "more than the lock"
    )

    # --- and the work resumes ---------------------------------------------------
    run = _at_super(superstudy, "iterate", "--study", MEMBER, "--batch", "1")
    assert (member / "derivatives" / f"{CHAIN}+{DATASET_ID}+{LABEL}").is_dir(), (
        f"the acknowledged member did not advance:\n{run.stderr}"
    )
    _assert_every_level_clean(superstudy, "the iterate after acknowledgment", MEMBER)

    # MEMBER_2 is left drifted on purpose: acknowledgment is per-member, so refreshing
    # one must not quietly move the others onto tools they have not been given.
    assert campaign_mod.uv_lock_path(superstudy / MEMBER_2, LABEL).read_text() != (
        canonical.read_text()
    ), "refreshing one member refreshed its sibling"


def _stage_campaign_init_adopts_the_superstudy(
    superstudy, cluster_config, apps, mechababs_pin, babs_pin
):
    """A second campaign in the same superstudy: adoption, not creation.

    A superstudy accumulates campaigns over time — each its own label and config
    epoch — so pointing `--superstudy` at one that already exists must adopt it as it
    stands rather than refuse or re-create. This is the path a user takes on every
    campaign after their first.

    Last, deliberately: it builds a second venv, and a failure here should not mask
    the spine above it.
    """
    _run(
        [
            _driver_mechababs(),
            "campaign",
            "init",
            ADOPTED_LABEL,
            "--superstudy",
            superstudy.name,
            "--apps",
            str(apps / f"{ANCHOR}.yaml"),
            "--cluster",
            str(cluster_config),
            *(["--mechababs", mechababs_pin] if mechababs_pin else []),
            *(["--babs", babs_pin] if babs_pin else []),
            "--limit",
            "1",
        ],
        cwd=superstudy.parent,
    )

    # Adopted as it stands, campaigns and all: the new one is beside the old, and the
    # old one's catalog — the record of what it has been doing — is untouched.
    assert campaign_mod.members_path(superstudy, ADOPTED_LABEL).is_file()
    assert _member_rows(superstudy, ADOPTED_LABEL) == [], (
        "the adopted campaign inherited the other campaign's members"
    )
    assert [r["study"] for r in _member_rows(superstudy)] == [MEMBER, MEMBER_2], (
        "adopting a superstudy for a new campaign disturbed the existing one"
    )
    _assert_every_level_clean(superstudy, "adoption", MEMBER, MEMBER_2)


def test_superstudy(
    workdir,
    study_template,
    cluster_config,
    app_configs,
    mechababs_pin,
    babs_pin,
    simbids_sif,
    tmp_path,
):
    """The superstudy layer, in order. Add later stages to the bottom.

    The superstudy is created by the code under test, so the fixture only chooses a
    name that is not taken — `campaign init --superstudy` making the dataset is the
    create path being exercised, not setup being skipped.
    """
    superstudy = workdir / f"e2e-super-{os.urandom(4).hex()}"
    apps = _localized_app_configs(
        app_configs, tmp_path / "apps", simbids_sif.parent.parent.parent
    )

    _stage_superstudy_init(superstudy, cluster_config, apps, mechababs_pin, babs_pin)
    _clone_member(superstudy, study_template, MEMBER)
    _stage_add_dataset_needs_a_member(superstudy)
    _stage_add_dataset_selects_a_member(superstudy)
    _stage_a_member_is_not_operated_from(superstudy)
    _stage_fanout_scaffold(superstudy)
    _stage_fanout_submit_and_merge(superstudy)
    _stage_a_second_member_and_narrowing(superstudy, study_template)
    _stage_a_drifted_member_is_refused_until_acknowledged(superstudy)
    _stage_campaign_init_adopts_the_superstudy(
        superstudy, cluster_config, apps, mechababs_pin, babs_pin
    )
