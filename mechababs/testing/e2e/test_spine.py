"""The spine: `campaign init` -> `add-dataset` -> scaffold -> submit -> merge.

One test, run as ordered **stages**, against a real study on a real filesystem. It
asserts the things only an end-to-end run can: that `uv lock` + `uv sync` actually
resolve a campaign environment, that the committed `env.sh` really selects and
activates it in a fresh shell, that the env-match guard really refuses the wrong
python, that real jobs reach a real scheduler and their results land in a
derivative, and that what landed in the study's git history is what should have.

**Two cells, driven two ways.** The anchor cell's transitions are dispatched by hand
(`_dispatch`), one named verb at a time, which is how a verb and its self-guards are
tested. The chain cell is driven by `mechababs iterate` alone (`_iterate`) — nobody
names a verb; the reconciler reads the shard and decides. Both are the real path, and
between them the whole loop a user actually runs is covered.

**It grows by appending stages, not by rewriting.** Each `_stage_*` takes the study
and returns nothing but assertions; the driver below calls them in order. Keeping it
one test (rather than one test per stage) is deliberate: the stages share one study,
and a later stage is meaningless if an earlier one failed, so a cascade of red for a
single cause is noise.

**Two rungs, split at `submit`.** Everything up to and including `scaffold` is
`babs init` and git — no scheduler, so it runs on a developer's host in a couple of
minutes, and that fast loop is worth protecting. From `submit` on, real jobs run, so
those stages need `sbatch` and are skipped-with-a-reason where there is none (see
`_skip_without_scheduler`); the rung that runs them is `run_in_podman.sh`, or a real
cluster login node. The fixture study and the container dataset are cached between
runs either way.
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

import pytest
import yaml

from conftest import BUMP_PACKAGE, bump_declaration
from mechababs import babs_status
from mechababs import campaign as campaign_mod
from mechababs import status as status_mod

log = logging.getLogger("mechababs.e2e")

LABEL = "e2e"

# How long the merge stage waits for the cell's jobs. simbids jobs take seconds, so
# this is a stuck-detector, not a budget: generous enough that a loaded scheduler
# does not fail the suite, short enough that a hung job is not an infinite wait.
JOB_WAIT_SECONDS = 900
JOB_POLL_SECONDS = 10

# The suite's two SimBIDS app configs, in bundle order. The second declares
# `depends_on: <the first>`, so the bundle carries a real topology edge for
# add-dataset to resolve — and, later, for scaffold to gate on.
ANCHOR = "SimBIDS-0.0.3+anchor"
CHAIN = "SimBIDS-0.0.3+chain"

# The fixture study's sentinel dataset (conftest's DATASET_ID). It is a NAMED
# sourcedata slot, not a generic `raw`/`rawbids` one, so the derivatives scaffold
# produces carry the source id — the collision-proof half of the naming rule.
DATASET_ID = "ds999999"
SOURCEDATA = f"sourcedata/{DATASET_ID}"


# --------------------------------------------------------------------------
# Driving the CLI
# --------------------------------------------------------------------------


def _run(cmd, cwd, *, env=None, check=True):
    """Run a command, log it, and return the completed process.

    Output is captured rather than streamed so a stage can assert on the message a
    guard printed; it is logged either way, so `-s` still shows the run.
    """
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
    """The `mechababs` running this scenario — the one that creates the campaign.

    `campaign init` is the one verb that runs *before* a campaign environment exists
    (in prod, `uvx --from git+…`), so it necessarily comes from outside the campaign.
    Here that is the install the suite itself is running from — found beside the
    running interpreter, not on PATH, where a stray host install of mechababs would
    shadow the code under test.
    """
    exe = Path(sys.executable).parent / "mechababs"
    assert exe.is_file(), (
        f"no `mechababs` beside {sys.executable} — the code under test is not "
        "installed in the environment running this suite"
    )
    return str(exe)


def _in_campaign(study, label, *args, check=True):
    """Run an operating verb the way a user does: source `env.sh`, then the verb.

    Not by calling the campaign venv's binary directly. Sourcing is the documented
    entry point and the only thing that sets `MECHABABS_CAMPAIGN`, so driving it any
    other way would leave the select-and-activate step — the half most likely to
    break — untested.
    """
    env_sh = campaign_mod.env_path(study, label)
    script = f'. "{env_sh}" && mechababs ' + " ".join(f'"{a}"' for a in args)
    return _run(["bash", "-c", script], study, check=check)


def _dispatch(study, verb, source_dataset, app_config, *, check=True):
    """Dispatch ONE named cell's transition, choosing it by hand.

    The scenario drives cells both ways, on purpose. This one names the verb and the
    cell, which is how a verb — and each of its self-guards — is tested in isolation:
    the anchor cell's whole life goes through here. `_iterate` is the other way, where
    nobody chooses and the reconciler decides; the chain cell goes through that.

    It calls the dispatcher from inside the campaign venv, which is exactly where
    `iterate` calls it from, so what runs is the real thing either way: a `datalad
    run` at the study invoking the pinned `mechababs-inner`.
    """
    env_sh = campaign_mod.env_path(study, LABEL)
    script = (
        f'. "{env_sh}" && python -c '
        f"'import sys; from mechababs import dispatch; "
        f"getattr(dispatch, sys.argv[1])(*sys.argv[2:])' "
        f'"$1" "$2" "$3" "$4" "$5"'
    )
    return _run(
        [
            "bash",
            "-c",
            script,
            "e2e-dispatch",
            verb,
            str(study),
            LABEL,
            source_dataset,
            app_config,
        ],
        study,
        check=check,
    )


def _babs_status(study, project):
    """`babs status --json` from inside the campaign, parsed the way mechababs does.

    Deliberately the same one-shot `json.loads` of the whole stdout that
    `babs_status.read_status` does: if babs ever prints anything alongside its JSON,
    that breaks the reconciler, and a more forgiving parser here would hide it.
    """
    env_sh = campaign_mod.env_path(study, LABEL)
    proc = _run(
        ["bash", "-c", f'. "{env_sh}" && babs status --json "$1"', "e2e", str(project)],
        study,
    )
    return json.loads(proc.stdout)


def _wait_for_jobs(study, project):
    """Poll until the cell's jobs have all ended, and return the final counts.

    Bounded: simbids jobs are seconds, so a long wait means something is stuck, and
    hanging a suite forever is a worse failure mode than a wrong answer. Polls the
    same decision seam the reconciler routes on — anything but "skip" means the
    jobs have stopped moving, and the caller decides whether that is a merge.
    """
    deadline = time.monotonic() + JOB_WAIT_SECONDS
    while True:
        status = _babs_status(study, project)
        action = babs_status.decide(status)
        log.info("babs status: %s -> %s", status, action)
        if action != "skip":
            return status
        assert time.monotonic() < deadline, (
            f"jobs still in flight after {JOB_WAIT_SECONDS}s: {status}"
        )
        time.sleep(JOB_POLL_SECONDS)


def _skip_without_scheduler(stage):
    """True (with the reason logged) when this rung cannot run jobs.

    The stages from `submit` on need a real scheduler, so they run on the podman
    rung and on a cluster, not on a developer's host. Skipping *within* the test
    rather than skipping the test keeps the earlier stages green on the host, which
    is what makes them a fast loop worth having.

    `sbatch` on PATH is the question being asked — not "am I in a container".
    """
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


def _assert_clean(study, phase):
    assert not _git(study, "status", "--porcelain").strip(), (
        f"study dirty after {phase} — mechababs left work uncommitted:\n"
        + _git(study, "status", "--porcelain")
    )


def _state_rows(study, label):
    with open(campaign_mod.state_path(study, label), newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def _run_record(study):
    """The `datalad run` record datalad embeds as JSON in the HEAD commit's body.

    This is the artifact dispatch exists to produce, so the scenario reads it rather
    than trusting the commit subject: the subject says a run happened, the record
    says *which command*, from *where*, declaring *what*.
    """
    body = _git(study, "log", "-1", "--format=%b")
    return json.loads(body[body.index("{") : body.rindex("}") + 1])


def _iterate(study, *args):
    """One iterate, run the way a user runs it: sourced env.sh, then `iterate`.

    No path and no cell named: where you stand is the study, the env var is the
    campaign, and which cell moves is the reconciler's decision, not ours. That
    is the whole difference between this and `_dispatch`.
    """
    return _in_campaign(study, LABEL, "iterate", *args)


def _status(study):
    return _in_campaign(study, LABEL, "status").stdout


def _status_row(table, app):
    """One row of the `status` table, parsed by the header's column positions.

    The table is aligned, not delimited, and its values carry spaces ("not started",
    "waiting on X"), so `split()` would tear them. Alignment means the header line
    holds every column's start, so that is what this reads it by.
    """
    lines = table.splitlines()
    starts, pos = [], 0
    for col in status_mod.COLUMNS:
        pos = lines[0].index(col, pos)
        starts.append(pos)
        pos += len(col)
    bounds = list(zip(starts, starts[1:] + [None]))
    for line in lines[1:]:
        fields = {
            col: line[a:b].strip() for col, (a, b) in zip(status_mod.COLUMNS, bounds)
        }
        if fields["app"] == app:
            return fields
    raise AssertionError(f"no row for {app} in:\n{table}")


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------


def _stage_campaign_init(study, cluster_config, app_configs, mechababs_pin, babs_pin):
    """`campaign init` builds the campaign footprint and its environment.

    Either way the campaign records the code under test. `--mechababs` is passed when
    the caller named one (a dev run, whose checkout is a mount path `campaign init`
    could not have inferred) and omitted otherwise, which is how `test-cluster` runs:
    init then self-pins the mechababs executing it, exactly as a user's own
    `campaign init` does.
    """
    _run(
        [
            _driver_mechababs(),
            "campaign",
            "init",
            LABEL,
            "--apps",
            f"{app_configs / f'{ANCHOR}.yaml'},{app_configs / f'{CHAIN}.yaml'}",
            "--cluster",
            str(cluster_config),
            *(["--mechababs", mechababs_pin] if mechababs_pin else []),
            *(["--babs", babs_pin] if babs_pin else []),
            "--limit",
            "1",
        ],
        cwd=study,
    )

    campaign = campaign_mod.campaign_dir(study, LABEL)
    assert campaign.is_dir(), f"campaign not created at {campaign}"

    # The configs are COPIED in, under the campaign's own directories: the config
    # that produced a run is committed in the study, so the run reproduces from the
    # study alone.
    for name in (ANCHOR, CHAIN):
        assert (campaign_mod.apps_dir(study, LABEL) / f"{name}.yaml").is_file(), (
            f"{name} was not copied into the campaign"
        )
    assert (campaign_mod.clusters_dir(study, LABEL) / cluster_config.name).is_file(), (
        "the cluster config was not copied into the campaign"
    )

    config = yaml.safe_load(campaign_mod.config_path(study, LABEL).read_text())
    assert config["label"] == LABEL
    assert config["apps"] == [
        f"{campaign_mod.APPS_DIRNAME}/{ANCHOR}.yaml",
        f"{campaign_mod.APPS_DIRNAME}/{CHAIN}.yaml",
    ], "campaign.yaml lost the bundle or its order"
    assert config["limit"] == 1, "--limit did not reach campaign.yaml"

    # The lock is the provenance record: it must exist, be committed, and name the
    # mechababs under test.
    lock = campaign_mod.uv_lock_path(study, LABEL)
    assert lock.is_file(), "campaign init produced no uv.lock"
    assert "mechababs" in lock.read_text()

    # An environment that was really built, not just declared.
    venv = campaign_mod.venv_path(study, LABEL)
    assert (venv / "bin" / "mechababs").is_file(), (
        "the campaign venv has no mechababs — uv sync did not install the pin"
    )

    # Header only. Which source datasets a campaign acts on is add-dataset's
    # explicit, separate step — never implied by init.
    assert _state_rows(study, LABEL) == [], (
        "campaign init wrote cells; selection belongs to add-dataset"
    )
    assert (
        campaign_mod.state_path(study, LABEL).read_text()
        == campaign_mod.initial_header()
    )

    _assert_clean(study, "campaign init")


def _stage_env_sh_selects_and_activates(study):
    """Sourcing `env.sh` is what makes an operating verb runnable — and nothing else is.

    Two directions, because the env-match guard exists to refuse both: the sourced
    shell passes, and the driver's own mechababs — the very install that just created
    the campaign — is refused even with `MECHABABS_CAMPAIGN` hand-set. That negative
    is the one that matters: it is the wrong-tools-recorded-a-run bug.
    """
    sourced = _in_campaign(study, LABEL, "--version")
    assert sourced.stdout.startswith("mechababs"), sourced.stdout

    env_sh = campaign_mod.env_path(study, LABEL)
    which = _run(
        [
            "bash",
            "-c",
            f'. "{env_sh}" && echo "$MECHABABS_CAMPAIGN" && command -v mechababs',
        ],
        study,
    )
    label, exe = which.stdout.split()
    assert label == LABEL, f"env.sh selected {label!r}, not {LABEL!r}"
    # Resolved on both sides: env.sh derives the venv from its own location with
    # `cd … && pwd`, which resolves symlinks — and a workdir reached through one
    # (a scratch symlink is normal on a cluster) would otherwise fail a string compare.
    expected = (campaign_mod.venv_path(study, LABEL) / "bin" / "mechababs").resolve()
    assert Path(exe).resolve() == expected, (
        f"env.sh activated something other than the campaign venv: {exe}"
    )

    refused = _run(
        [_driver_mechababs(), "add-dataset", "--sourcedata", SOURCEDATA],
        cwd=study,
        env={**os.environ, campaign_mod.CAMPAIGN_ENV_VAR: LABEL},
        check=False,
    )
    assert refused.returncode != 0, (
        "an un-sourced mechababs was allowed to operate on the campaign"
    )
    assert "not running in the venv" in refused.stderr, refused.stderr


def _stage_add_dataset(study):
    """`add-dataset` writes the cells: this source dataset x the campaign's apps."""
    _in_campaign(study, LABEL, "add-dataset", "--sourcedata", SOURCEDATA)

    rows = _state_rows(study, LABEL)
    assert len(rows) == 2, f"expected one cell per app, got {len(rows)}: {rows}"

    anchor, chain = rows
    assert [r["app_config"] for r in rows] == [
        f"{campaign_mod.APPS_DIRNAME}/{ANCHOR}.yaml",
        f"{campaign_mod.APPS_DIRNAME}/{CHAIN}.yaml",
    ], "cells are not one per app, in bundle order"

    for row in rows:
        # Identity, sniffed from the study's own metadata TSV. The phantom is
        # single-session, so it is subject-level and n_sessions is BLANK — not 0,
        # which would read as "sessions, none of them".
        assert row["source_dataset"] == SOURCEDATA, (
            "source_dataset is not the study-relative path the user named"
        )
        assert row["processing_level"] == "subject", row
        assert int(row["n_subjects"]) > 0, "the sniff found no subjects"
        assert row["n_sessions"] == "", (
            "a subject-level dataset reported a session count"
        )
        # Derived columns empty is what makes the next iterate scaffold the cell.
        assert row["babs"] == "" and row["merged"] == "", row

    assert anchor["depends_on"] == "", "the anchor app declares no dependency"
    assert chain["depends_on"] == f"{campaign_mod.APPS_DIRNAME}/{ANCHOR}.yaml", (
        "depends_on was not resolved from the declared stem to the producer's "
        f"config path: {chain['depends_on']!r}"
    )

    # The commit is path-scoped to the campaign dir: mechababs' change to a study is
    # additive, so nothing upstream authored is touched by an add.
    touched = _git(study, "show", "--pretty=", "--name-only", "HEAD").split()
    campaign_rel = campaign_mod.campaign_dir(study, LABEL).relative_to(study).as_posix()
    assert touched, "add-dataset committed nothing"
    assert all(p.startswith(campaign_rel) for p in touched), (
        f"add-dataset's commit reaches outside the campaign dir: {touched}"
    )

    _assert_clean(study, "add-dataset")

    # A dataset is selected whole or not at all — the app bundle is fixed at init, so
    # re-adding refuses rather than rewriting or duplicating cells.
    again = _in_campaign(
        study, LABEL, "add-dataset", "--sourcedata", SOURCEDATA, check=False
    )
    assert again.returncode != 0, "re-adding a selected dataset was allowed"
    assert "already selected" in again.stderr, again.stderr
    assert len(_state_rows(study, LABEL)) == 2, "the refused re-add still wrote rows"


def _stage_history(study):
    """The study's git history is the orchestration record — assert its shape.

    First-parent, most recent first: the add, then the init, then whatever the
    fixture study already had. Each mechababs transition is one attributable node.
    """
    subjects = _git(study, "log", "--first-parent", "--format=%s").splitlines()
    assert subjects[0].startswith(f"mechababs add-dataset {SOURCEDATA}"), subjects
    assert subjects[1].startswith(f"mechababs campaign init {LABEL}"), subjects
    assert len(subjects) > 2, "the fixture study's own history is gone"


def _stage_scaffold(study):
    """The first mutating transition: `babs init` a real derivative, recorded as a run.

    Everything a scaffold owns is asserted here — the derivative in its final home
    and registered as a subdataset, the inclusion pinned beside the statefile, the
    cell recorded — plus the two things that make it *provenance*: the study's HEAD
    is a run record, and the command in it is study-relative, so it re-executes
    somewhere other than this machine.
    """
    anchor_app = f"{campaign_mod.APPS_DIRNAME}/{ANCHOR}.yaml"
    _dispatch(study, "scaffold", SOURCEDATA, anchor_app)

    # The derivative is created in its final home inside the study — nothing is
    # composed or relocated afterwards, which is what keeps run provenance clean.
    # It carries the source id because the sourcedata slot is a named one.
    derivative = study / "derivatives" / f"{ANCHOR}+{DATASET_ID}+{LABEL}"
    assert derivative.is_dir(), f"no derivative at {derivative}"
    assert (derivative / ".babs").is_dir(), "not a babs project — babs init did not run"
    assert (derivative / "code" / "processing_inclusion.csv").is_file(), (
        "babs recorded no inclusion; --list-sub-file never reached it"
    )

    # Registered as a real subdataset of the study, not a stray directory: that
    # registration is the study's record that this derivative is part of it.
    gitlink = _git(study, "ls-tree", "HEAD", str(derivative.relative_to(study))).split()
    assert gitlink[:2] == ["160000", "commit"], (
        f"the derivative is not registered as a subdataset: {gitlink}"
    )

    # The RIA stores are babs's local machinery, not content: committing them would
    # put an absolute-path store into the published derivative. Scoped to the two
    # store directories, since babs does track `.babs/babs_init_config.yaml` itself.
    assert not _git(
        derivative, "ls-files", "--", ".babs/input_ria", ".babs/output_ria"
    ).strip(), "the RIA stores were committed into the derivative"

    # The pin records what was REQUESTED; babs's own processing_inclusion.csv records
    # what it could run. Their diff is what catches a selected subject the data lacks.
    pin = (
        campaign_mod.inclusions_dir(study, LABEL)
        / f"{SOURCEDATA.replace('/', '-')}_{ANCHOR}.csv"
    )
    assert pin.is_file(), f"no inclusion pinned at {pin}"
    requested = pin.read_text().split()
    assert requested[0] == "sub_id" and len(requested) == 2, (
        f"--limit 1 should pin exactly one subject, got {requested}"
    )

    # The cell's durable fact, and only that cell's.
    rows = {r["app_config"]: r for r in _state_rows(study, LABEL)}
    assert rows[anchor_app]["babs"] == f"derivatives/{ANCHOR}+{DATASET_ID}+{LABEL}", (
        rows
    )
    assert rows[anchor_app]["merged"] == "", "scaffold claimed a merge"
    assert rows[f"{campaign_mod.APPS_DIRNAME}/{CHAIN}.yaml"]["babs"] == "", (
        "scaffolding one cell advanced its sibling"
    )

    # The point of dispatch: the transition landed as a re-executable command, not
    # as a save with an adjective on it.
    subject = _git(study, "log", "-1", "--format=%s").strip()
    assert subject.startswith("[DATALAD RUNCMD] mechababs scaffold"), subject
    record = _run_record(study)
    assert record["pwd"] == ".", record
    assert record["cmd"] == (
        f"mechababs-inner scaffold --campaign {LABEL} "
        f"--source-dataset {SOURCEDATA} --app {anchor_app}"
    ), record["cmd"]
    assert str(study) not in record["cmd"], (
        "the recorded command carries this machine's path, so it re-executes nowhere"
    )

    # Declared outputs, so this also says nothing undeclared was swept in.
    assert set(record["outputs"]) == {
        f"derivatives/{ANCHOR}+{DATASET_ID}+{LABEL}",
        str(campaign_mod.state_path(study, LABEL).relative_to(study)),
        str(pin.relative_to(study)),
        ".gitmodules",
    }, record["outputs"]

    _assert_clean(study, "scaffold")

    # The self-guard: the recorded command re-run against a cell that has since been
    # scaffolded must fail loudly, not init a second derivative over the first.
    again = _dispatch(study, "scaffold", SOURCEDATA, anchor_app, check=False)
    assert again.returncode != 0, "a scaffolded cell was scaffolded again"
    assert "already scaffolded" in again.stderr, again.stderr
    _assert_clean(study, "the refused re-scaffold")


def _stage_dependent_cell_waits_for_its_producer(study):
    """A cell is scaffolded only after its producer's results are merged.

    The anchor is initialized but nothing has run, let alone merged, so the chain
    cell is not ready — and this verb, which is only ever reached because something
    decided a cell WAS ready, has to say so rather than proceed.
    """
    chain_app = f"{campaign_mod.APPS_DIRNAME}/{CHAIN}.yaml"
    refused = _dispatch(study, "scaffold", SOURCEDATA, chain_app, check=False)

    assert refused.returncode != 0, "a dependent cell scaffolded before its producer"
    assert "not merged yet" in refused.stderr, refused.stderr
    rows = {r["app_config"]: r for r in _state_rows(study, LABEL)}
    assert rows[chain_app]["babs"] == "", "the refused cell was recorded anyway"
    assert not (study / "derivatives" / f"{CHAIN}+{DATASET_ID}+{LABEL}").exists()
    _assert_clean(study, "the refused dependent cell")

    # This is the one moment the waiting state exists — the producer scaffolded, not
    # merged — so it is where `status` gets asserted for it. (Only the dependent's row:
    # the producer's is an active cell, whose live counts need a scheduler this rung
    # may not have.)
    assert _status_row(_status(study), CHAIN)["state"] == f"waiting on {ANCHOR}", (
        _status(study)
    )


def _stage_submit(study):
    """Jobs reach the scheduler — and the study's history does not notice.

    Submit is the one transition run WITHOUT a `datalad run`, because babs's job
    bookkeeping is gitignored inside the derivative and nothing tracked moves. That
    claim is what this stage checks against a real babs and a real scheduler: HEAD
    where it was, tree clean, statefile byte-identical, and the counts moved.
    """
    if _skip_without_scheduler("_stage_submit"):
        return
    anchor_app = f"{campaign_mod.APPS_DIRNAME}/{ANCHOR}.yaml"
    chain_app = f"{campaign_mod.APPS_DIRNAME}/{CHAIN}.yaml"
    project = study / "derivatives" / f"{ANCHOR}+{DATASET_ID}+{LABEL}"

    head = _git(study, "rev-parse", "HEAD").strip()
    statefile = campaign_mod.state_path(study, LABEL).read_text()

    _dispatch(study, "submit", SOURCEDATA, anchor_app)

    status = _babs_status(study, project)
    assert status["total"] > 0, f"babs knows of no jobs to submit: {status}"
    assert status["submitted"] == status["total"], (
        f"submit left jobs undeployed: {status}"
    )

    # The determination that makes submit a plain verb, asserted end to end.
    assert _git(study, "rev-parse", "HEAD").strip() == head, (
        "submit committed — it is dispatched plainly on the grounds that it "
        "changes nothing tracked"
    )
    _assert_clean(study, "submit")
    assert campaign_mod.state_path(study, LABEL).read_text() == statefile, (
        "submit wrote to the statefile; submitted-ness is babs's, queried live"
    )

    # The self-guard, from the other direction: the chain cell has no babs project,
    # so there is nothing to submit and saying so must be loud.
    refused = _dispatch(study, "submit", SOURCEDATA, chain_app, check=False)
    assert refused.returncode != 0, "an unscaffolded cell was submitted"
    assert "not scaffolded" in refused.stderr, refused.stderr


def _stage_merge(study):
    """The cell finishes: results consolidated into the derivative, cell recorded done.

    The one stage that proves a derivative was actually *produced* — everything
    before it builds machinery. It waits on the real jobs, merges, and then asserts
    both halves of what merge owns: content in the derivative, and a run record in
    the study saying which command put it there.
    """
    if _skip_without_scheduler("_stage_merge"):
        return
    anchor_app = f"{campaign_mod.APPS_DIRNAME}/{ANCHOR}.yaml"
    chain_app = f"{campaign_mod.APPS_DIRNAME}/{CHAIN}.yaml"
    derivative = f"derivatives/{ANCHOR}+{DATASET_ID}+{LABEL}"
    project = study / derivative

    status = _wait_for_jobs(study, project)
    assert babs_status.decide(status) == "merge", (
        f"the jobs did not all succeed, so there is nothing to merge: {status}"
    )

    _dispatch(study, "merge", SOURCEDATA, anchor_app)

    rows = {r["app_config"]: r for r in _state_rows(study, LABEL)}
    assert rows[anchor_app]["merged"] == "true", rows[anchor_app]
    assert rows[chain_app]["merged"] == "", "merging one cell advanced its sibling"

    # `babs merge` consolidates in the output RIA; the derivative only carries the
    # results because merge fast-forwards it onto that branch afterwards. Tracked
    # files, so this is the derivative's own committed content, not stray output.
    tracked = _git(project, "ls-files").split()
    produced = [p for p in tracked if p.startswith("sub-") and p.endswith(".zip")]
    assert produced, (
        "the derivative carries no per-subject results — babs merged into the RIA "
        f"and the derivative was not fast-forwarded onto it:\n{tracked}"
    )

    subject = _git(study, "log", "-1", "--format=%s").strip()
    assert subject.startswith("[DATALAD RUNCMD] mechababs merge"), subject
    record = _run_record(study)
    assert record["pwd"] == ".", record
    assert record["cmd"] == (
        f"mechababs-inner merge --campaign {LABEL} "
        f"--source-dataset {SOURCEDATA} --app {anchor_app}"
    ), record["cmd"]
    # Two, and no `.gitmodules`: merge registers and drops nothing at the study.
    assert set(record["outputs"]) == {
        derivative,
        str(campaign_mod.state_path(study, LABEL).relative_to(study)),
    }, record["outputs"]

    _assert_clean(study, "merge")

    # The self-guard: rerunning the recorded command against a cell that is now
    # merged must fail loudly rather than merge a second time.
    again = _dispatch(study, "merge", SOURCEDATA, anchor_app, check=False)
    assert again.returncode != 0, "a merged cell was merged again"
    assert "already merged" in again.stderr, again.stderr
    _assert_clean(study, "the refused re-merge")


def _stage_update_env_bumps_the_environment(study):
    """The mid-campaign bump, for real: edit the declaration, converge, keep going.

    This is the one path where a campaign's environment changes under a running
    campaign, and it is the reason `update-env` exists. Everything about it is
    hand-edit-then-converge: there is no bump flag, so the scenario edits
    `pyproject.toml` in the text the way a user does, and `update-env` re-resolves
    whatever it now says.

    Placed after merge, where a campaign has real history to disturb: a merged cell
    behind it and an unstarted one ahead. That ordering is what makes the last two
    assertions worth anything — the environment moves *between* two cells' lifetimes,
    which is exactly the heterogeneity the design accepts and records.

    Needs no scheduler: this is uv and git.
    """
    campaign = campaign_mod.campaign_dir(study, LABEL)
    lock = campaign_mod.uv_lock_path(study, LABEL)
    env_sh = campaign_mod.env_path(study, LABEL)

    def importable(package):
        return (
            _run(
                ["bash", "-c", f'. "{env_sh}" && python -c "import {package}"'],
                study,
                check=False,
            ).returncode
            == 0
        )

    before_lock = lock.read_text()
    assert not importable(BUMP_PACKAGE), (
        f"{BUMP_PACKAGE} is already in the campaign venv, so this stage would "
        "prove nothing about the sync"
    )

    bump_declaration(campaign)
    # The declaration is DIRTY now, and deliberately so: hand-edit-then-converge is
    # the documented bump, so update-env has to accept a dirty pyproject rather than
    # refuse it the way every other writing verb refuses a dirty scope.
    assert _git(study, "status", "--porcelain").strip(), "the hand-edit changed nothing"

    _in_campaign(study, LABEL, "campaign", "update-env")

    # 1. The lock moved, and now names the package the declaration asked for.
    assert lock.read_text() != before_lock, "update-env did not re-resolve the lock"
    assert f'name = "{BUMP_PACKAGE}"' in lock.read_text(), (
        f"the re-resolved lock does not carry {BUMP_PACKAGE}"
    )
    # 2. The venv really gained it — the sync ran, not just the resolve.
    assert importable(BUMP_PACKAGE), (
        f"the lock names {BUMP_PACKAGE} but the campaign venv cannot import it: "
        "update-env resolved without installing"
    )
    # 3. Exactly the two environment files, in one commit. The user's edit and the
    #    resolution it produced belong together, and nothing else is swept in.
    changed = sorted(_git(study, "show", "--name-only", "--format=", "HEAD").split())
    assert changed == sorted(
        [
            str(campaign_mod.pyproject_path(study, LABEL).relative_to(study)),
            str(lock.relative_to(study)),
        ]
    ), (
        f"update-env committed something other than the declaration and its lock:\n{changed}"
    )
    subject = _git(study, "log", "-1", "--format=%s").strip()
    assert subject.startswith("mechababs campaign update-env"), subject
    # A plain save, NOT a `datalad run`: `uv lock` resolves against the live world,
    # so a re-executable record of it would be a promise the command cannot keep.
    assert not subject.startswith("[DATALAD RUNCMD]"), (
        f"update-env recorded itself as a re-executable run: {subject}"
    )
    _assert_clean(study, "update-env")

    # 4. The outer guard accepts the converged environment. It runs `uv sync --check`
    #    against the venv it just built, so this is the first proof that the two
    #    halves of the bump agree — a lock that moved without its venv would refuse
    #    every verb from here on.
    run = _iterate(study, "--dry-run")
    assert run.returncode == 0, run.stderr
    assert "does not match" not in run.stderr, run.stderr

    # 5. And an inner verb still dispatches under the new lock. Re-dispatching merge
    #    on the merged anchor is the safe way to ask: if the study-local env check
    #    refused, it would fail there; instead it reaches the cell-state guard and
    #    fails for the *cell's* reason, which is the answer we want.
    if shutil.which("sbatch"):
        anchor_app = f"{campaign_mod.APPS_DIRNAME}/{ANCHOR}.yaml"
        again = _dispatch(study, "merge", SOURCEDATA, anchor_app, check=False)
        assert again.returncode != 0
        assert "already merged" in again.stderr, (
            "the inner verb did not reach its cell-state guard under the bumped "
            f"lock:\n{again.stderr}"
        )
        _assert_clean(study, "the inner verb dispatched under the bumped lock")

    # The stage that follows drives the chain cell's whole life with real iterates, and
    # now does so under THIS lock — so the campaign ends deliberately heterogeneous,
    # one cell produced before the bump and one after, which is the honest record the
    # design is after rather than a defect.


def _stage_iterate_drives_the_chain_cell(study):
    """The reconciler, end to end: one cell's whole life, driven by `iterate` alone.

    Every stage above dispatched a transition by hand, because that is how a verb is
    tested. This is the other half — nobody chooses the transition. `iterate` reads
    the shard, works out what each cell is owed, and dispatches it; the operator only
    says "again".

    So the chain cell is deliberately left unscaffolded by the stages above, and gets
    its scaffold, its submit and its merge from three iterates. That also makes this the
    first run of the input-wiring path (the dependent's `input_datasets` entry names
    the producer's app, so scaffold resolves it to that cell's merged output store and
    hands babs the URL the YAML cannot carry) — asserted here, where it happens.
    """
    if _skip_without_scheduler("_stage_iterate_drives_the_chain_cell"):
        return
    chain_app = f"{campaign_mod.APPS_DIRNAME}/{CHAIN}.yaml"
    derivative = study / "derivatives" / f"{CHAIN}+{DATASET_ID}+{LABEL}"

    # Where the campaign stands before the reconciler touches it, as `status` sees it:
    # the anchor done, and the chain no longer waiting — merging the anchor is what
    # opened its gate, so it now reads as an ordinary not-started cell.
    table = _status(study)
    assert _status_row(table, ANCHOR)["state"] == "merged", table
    assert _status_row(table, CHAIN)["state"] == "not started", table

    # --- iterate 1: the gate is open, so the cell is scaffolded ------------
    run = _iterate(study)
    assert "not started -> scaffold" in run.stderr, run.stderr

    assert (derivative / ".babs").is_dir(), f"no babs project at {derivative}"
    rows = {r["app_config"]: r for r in _state_rows(study, LABEL)}
    assert rows[chain_app]["babs"] == f"derivatives/{CHAIN}+{DATASET_ID}+{LABEL}", rows

    # The tick was a real `datalad run` — iterate itself is a plain
    # coordinator, so what lands in the study is the verb's record, not iterate's.
    subject = _git(study, "log", "-1", "--format=%s").strip()
    assert subject.startswith("[DATALAD RUNCMD] mechababs scaffold"), subject
    record = _run_record(study)
    assert record["cmd"] == (
        f"mechababs-inner scaffold --campaign {LABEL} "
        f"--source-dataset {SOURCEDATA} --app {chain_app}"
    ), record["cmd"]

    # The config babs kept carries the producer's output RIA, in the alias form.
    babs_config = yaml.safe_load(
        (derivative / ".babs" / "babs_init_config.yaml").read_text()
    )
    origin = babs_config["input_datasets"][ANCHOR]["origin_url"]
    assert origin.startswith("ria+file://") and origin.endswith("output_ria#~data"), (
        f"the chained input is not wired to an output-RIA alias: {origin}"
    )
    assert f"derivatives/{ANCHOR}+{DATASET_ID}+{LABEL}/.babs/output_ria" in origin, (
        f"the chained input points somewhere other than the producer: {origin}"
    )
    # And babs resolved it: the producer's output is installed as an input
    # subdataset, which only works if that URL really cloned.
    assert f"sourcedata/{ANCHOR}" in (derivative / ".gitmodules").read_text(), (
        "babs did not register the producer's output as this cell's input"
    )
    _assert_clean(study, "the iterate that scaffolded the chain cell")

    # --- iterate 2: the cell is active with nothing submitted, so submit ---
    head = _git(study, "rev-parse", "HEAD").strip()
    run = _iterate(study)
    assert "-> submit" in run.stderr, run.stderr

    status = _babs_status(study, derivative)
    assert status["total"] > 0, f"babs knows of no jobs for the chain cell: {status}"
    assert status["submitted"] == status["total"], (
        f"the iterate left jobs undeployed: {status}"
    )
    assert _git(study, "rev-parse", "HEAD").strip() == head, (
        "the submitting iterate committed — submit is dispatched plainly because it "
        "changes nothing tracked"
    )
    _assert_clean(study, "the iterate that submitted")

    # The in-flight iterate — jobs running, so the cell is skipped — is deliberately NOT
    # asserted here: whether the jobs have ended by the time an iterate lands is the
    # scheduler's business, so the e2e version of that assertion is a race. It is a
    # unit test (`test_jobs_still_in_flight_are_left_alone`), where the counts are ours.
    _wait_for_jobs(study, derivative)

    # --- iterate 3: everything ended successfully, so merge ----------------
    run = _iterate(study)
    assert "-> merge" in run.stderr, run.stderr

    rows = {r["app_config"]: r for r in _state_rows(study, LABEL)}
    assert rows[chain_app]["merged"] == "true", rows[chain_app]
    subject = _git(study, "log", "-1", "--format=%s").strip()
    assert subject.startswith("[DATALAD RUNCMD] mechababs merge"), subject

    produced = [
        p
        for p in _git(derivative, "ls-files").split()
        if p.startswith("sub-") and p.endswith(".zip")
    ]
    assert produced, "the chain cell's derivative carries no per-subject results"
    _assert_clean(study, "the iterate that merged")

    # --- the terminal state: every cell merged, and an iterate is a no-op --
    done = _iterate(study)
    assert "0 cell(s) advanced" in done.stderr, done.stderr
    table = _status(study)
    assert [_status_row(table, name)["state"] for name in (ANCHOR, CHAIN)] == [
        "merged",
        "merged",
    ], table
    _assert_clean(study, "the iterate with nothing left to do")


def _stage_retire_clears_a_cell_so_it_can_be_redone(study):
    """A derivative leaves the study, its cell reopens, and an iterate really redoes it.

    Both modes, because they are different promises. `--path` has to leave a readable
    archive OUTSIDE the study — same dataset relocated, not a copy — while `--remove`
    has to leave nothing anywhere. And between them the assertion that makes retire
    worth having at all: after the reset, `iterate` scaffolds the cell again.

    Runs last, and deliberately so: it takes the campaign's derivatives away.

    Needs no scheduler. The anchor cell is scaffolded by `_stage_scaffold` on every
    rung, and a re-scaffold is `babs init` and git.
    """
    anchor_app = f"{campaign_mod.APPS_DIRNAME}/{ANCHOR}.yaml"
    derivative_rel = f"derivatives/{ANCHOR}+{DATASET_ID}+{LABEL}"
    derivative = study / derivative_rel
    # A sibling of the study, so the move is a rename rather than a copy — and so the
    # outside-the-study rule is exercised against a real neighbouring directory.
    attic = study.parent / f"{study.name}-retired"
    before_id = campaign_mod.dataset_id(derivative)
    assert before_id, f"{derivative} has no datalad-id to preserve"

    # Inside the study is refused, and refused without touching anything — a study is
    # a published object, and a retired attempt kept in it would travel with it.
    refused = _in_campaign(
        study,
        LABEL,
        "retire-derivative",
        derivative_rel,
        "--path",
        str(study / "attic"),
        check=False,
    )
    assert refused.returncode != 0, "a destination inside the study was accepted"
    assert "must be outside the study" in refused.stderr, refused.stderr
    assert derivative.is_dir(), "the refused retire moved the derivative anyway"
    _assert_clean(study, "the refused retire")

    # --- --path: the archive keeps the evidence --------------------------------
    _in_campaign(
        study, LABEL, "retire-derivative", derivative_rel, "--path", str(attic)
    )

    parked = attic / f"{study.name}-{ANCHOR}+{DATASET_ID}+{LABEL}-attempt-1"
    assert parked.is_dir(), sorted(p.name for p in attic.iterdir())
    assert (parked / "logs").is_dir(), "the archive lost the logs it exists to keep"
    assert campaign_mod.dataset_id(parked) == before_id, (
        "the archive is a copy, not the same dataset relocated"
    )
    # Readable where it landed: an archive whose git history cannot be opened is a
    # directory of files, and the history is half the evidence.
    assert _git(parked, "log", "-1", "--format=%s").strip()

    assert not derivative.exists(), "the derivative is still in the study"
    assert f"derivatives/{ANCHOR}" not in (study / ".gitmodules").read_text(), (
        "the study still registers the retired derivative"
    )

    rows = {r["app_config"]: r for r in _state_rows(study, LABEL)}
    assert rows[anchor_app]["babs"] == "" and rows[anchor_app]["merged"] == "", (
        f"retire did not reset the cell: {rows[anchor_app]}"
    )
    assert rows[anchor_app]["source_dataset"] == SOURCEDATA, (
        "the reset rewrote an identity column"
    )
    assert _status_row(_status(study), ANCHOR)["state"] == "not started", _status(study)

    # A plain save, NOT a `datalad run`: `--remove` destroys content and `--path`
    # names a host directory outside every dataset, so recording either as
    # re-executable would be a promise retire cannot keep.
    subject = _git(study, "log", "-1", "--format=%s").strip()
    assert subject.startswith(
        f"mechababs retire-derivative {derivative_rel} --path "
    ), subject
    assert not subject.startswith("[DATALAD RUNCMD]"), (
        f"retire recorded itself as a re-executable run: {subject}"
    )
    # One commit, declaring only what the study level owns.
    changed = sorted(_git(study, "show", "--name-only", "--format=", "HEAD").split())
    assert changed == sorted(
        [
            ".gitmodules",
            str(campaign_mod.state_path(study, LABEL).relative_to(study)),
            derivative_rel,
        ]
    ), changed
    _assert_clean(study, "retire --path")

    # --- the reset is real: an iterate scaffolds the cell again ----------------
    run = _iterate(study, "--batch", "1", "--app", ANCHOR)
    assert "not started -> scaffold" in run.stderr, run.stderr
    assert (derivative / ".babs").is_dir(), "the cell was not re-scaffolded"
    _assert_clean(study, "the iterate that re-scaffolded the retired cell")

    # --- --remove: nothing is kept ---------------------------------------------
    _in_campaign(study, LABEL, "retire-derivative", derivative_rel, "--remove")

    assert not derivative.exists(), "--remove left the derivative in place"
    assert sorted(p.name for p in attic.iterdir()) == [parked.name], (
        "--remove parked the derivative instead of deleting it"
    )
    rows = {r["app_config"]: r for r in _state_rows(study, LABEL)}
    assert rows[anchor_app]["babs"] == "", "--remove did not reset the cell"
    assert _git(study, "log", "-1", "--format=%s").strip() == (
        f"mechababs retire-derivative {derivative_rel} --remove (campaign {LABEL!r})"
    )
    _assert_clean(study, "retire --remove")


def test_spine(
    study, cluster_config, app_configs, mechababs_pin, babs_pin, simbids_sif
):
    """The whole spine, in order. New stages go at the bottom.

    `simbids_sif` is requested because `scaffold` really inits against that container
    dataset, and because a missing one means this cluster config could not run
    anything — better a loud skip at the top than a green run that proved less than
    it looks.
    """
    _stage_campaign_init(study, cluster_config, app_configs, mechababs_pin, babs_pin)
    _stage_env_sh_selects_and_activates(study)
    _stage_add_dataset(study)
    _stage_history(study)
    _stage_scaffold(study)
    _stage_dependent_cell_waits_for_its_producer(study)
    _stage_submit(study)
    _stage_merge(study)
    _stage_update_env_bumps_the_environment(study)
    _stage_iterate_drives_the_chain_cell(study)
    _stage_retire_clears_a_cell_so_it_can_be_redone(study)


def test_campaign_init_refuses_outside_a_study(
    tmp_path, cluster_config, app_configs, mechababs_pin
):
    """mechababs operates on a study that already exists, and never authors one.

    The cheapest possible end-to-end proof of that boundary: point `campaign init` at
    a plain directory and it must refuse, rather than helpfully making it a dataset.
    """
    proc = _run(
        [
            _driver_mechababs(),
            "campaign",
            "init",
            LABEL,
            "--apps",
            str(app_configs / f"{ANCHOR}.yaml"),
            "--cluster",
            str(cluster_config),
            *(["--mechababs", mechababs_pin] if mechababs_pin else []),
        ],
        cwd=tmp_path,
        check=False,
    )
    assert proc.returncode != 0, "campaign init created a campaign outside a study"
    assert "not a study" in proc.stderr, proc.stderr
    assert not (tmp_path / campaign_mod.MECHABABS_DIR).exists()


@pytest.fixture(autouse=True)
def _log_phase(request):
    log.info("=== %s ===", request.node.name)
