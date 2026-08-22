"""The study-first spine, end to current: `campaign init` -> `add-dataset` -> `scaffold`.

One test, run as ordered **stages**, against a real study on a real filesystem. It
asserts the things only an end-to-end run can: that `uv lock` + `uv sync` actually
resolve a campaign environment, that the committed `env.sh` really selects and
activates it in a fresh shell, that the env-match guard really refuses the wrong
python, and that what landed in the study's git history is what should have.

**It grows by appending stages, not by rewriting.** Each `_stage_*` takes the study
and returns nothing but assertions; the driver below calls them in order. As the
reconciler verbs land, `_stage_submit` and `_stage_merge` join the list and the
earlier stages are untouched. Keeping it one test (rather than one test per stage)
is deliberate: the stages share one study, and a later stage is meaningless if an
earlier one failed, so a cascade of red for a single cause is noise.

No babs *jobs* run yet — `scaffold` is `babs init`, which needs no scheduler — so
the round trip is the campaign environment build, one babs project, and a handful
of git commits. The fixture study and the container dataset are cached between
runs, which is what keeps the whole thing at the couple-of-minutes mark.
"""

import csv
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from mechababs import campaign as campaign_mod

log = logging.getLogger("mechababs.e2e")

LABEL = "e2e"

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
    proc = subprocess.run(cmd, cwd=str(cwd), env=env, text=True,
                          capture_output=True)
    if proc.stdout:
        log.info("stdout:\n%s", proc.stdout)
    if proc.stderr:
        log.info("stderr:\n%s", proc.stderr)
    if check:
        assert proc.returncode == 0, (
            f"{cmd[0]} failed ({proc.returncode}):\n{proc.stderr}")
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
        "installed in the environment running this suite")
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
    """Dispatch one cell's transition the way `iterate` will.

    `iterate` is the next chunk; until it exists the scenario calls the dispatcher
    itself — from inside the campaign venv, which is where `iterate` will call it
    from, so the thing under test (a `datalad run` at the study invoking the pinned
    `mechababs-inner`) is the real one either way.
    """
    env_sh = campaign_mod.env_path(study, LABEL)
    script = (f'. "{env_sh}" && python -c '
              f"'import sys; from mechababs import dispatch; "
              f"getattr(dispatch, sys.argv[1])(*sys.argv[2:])' "
              f'"$1" "$2" "$3" "$4" "$5"')
    return _run(["bash", "-c", script, "e2e-dispatch",
                 verb, str(study), LABEL, source_dataset, app_config],
                study, check=check)


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          text=True, capture_output=True).stdout


def _assert_clean(study, phase):
    assert not _git(study, "status", "--porcelain").strip(), (
        f"study dirty after {phase} — mechababs left work uncommitted:\n"
        + _git(study, "status", "--porcelain"))


def _state_rows(study, label):
    with open(campaign_mod.state_path(study, label), newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def _run_record(study):
    """The `datalad run` record datalad embeds as JSON in the HEAD commit's body.

    This is the artifact the whole chunk exists to produce, so the scenario reads it
    rather than trusting the commit subject: the subject says a run happened, the
    record says *which command*, from *where*, declaring *what*.
    """
    body = _git(study, "log", "-1", "--format=%b")
    return json.loads(body[body.index("{"):body.rindex("}") + 1])


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------

def _stage_campaign_init(study, cluster_config, app_configs, mechababs_pin, babs_pin):
    """`campaign init` builds the campaign footprint and its environment.

    The mechababs pin is handed in explicitly so the campaign records the code under
    test rather than whatever the driver install happens to be — the same value a
    dev run and a future `test-cluster` both supply, differing only in what it points
    at.
    """
    _run([
        _driver_mechababs(), "campaign", "init", LABEL,
        "--apps", f"{app_configs / f'{ANCHOR}.yaml'},{app_configs / f'{CHAIN}.yaml'}",
        "--cluster", str(cluster_config),
        "--mechababs", mechababs_pin,
        *(["--babs", babs_pin] if babs_pin else []),
        "--limit", "1",
    ], cwd=study)

    campaign = campaign_mod.campaign_dir(study, LABEL)
    assert campaign.is_dir(), f"campaign not created at {campaign}"

    # The configs are COPIED in, under the campaign's own directories: the config
    # that produced a run is committed in the study, so the run reproduces from the
    # study alone.
    for name in (ANCHOR, CHAIN):
        assert (campaign_mod.apps_dir(study, LABEL) / f"{name}.yaml").is_file(), (
            f"{name} was not copied into the campaign")
    assert (campaign_mod.clusters_dir(study, LABEL) / cluster_config.name).is_file(), (
        "the cluster config was not copied into the campaign")

    config = yaml.safe_load(campaign_mod.config_path(study, LABEL).read_text())
    assert config["label"] == LABEL
    assert config["apps"] == [f"{campaign_mod.APPS_DIRNAME}/{ANCHOR}.yaml",
                              f"{campaign_mod.APPS_DIRNAME}/{CHAIN}.yaml"], (
        "campaign.yaml lost the bundle or its order")
    assert config["limit"] == 1, "--limit did not reach campaign.yaml"

    # The lock is the provenance record: it must exist, be committed, and name the
    # mechababs under test.
    lock = campaign_mod.uv_lock_path(study, LABEL)
    assert lock.is_file(), "campaign init produced no uv.lock"
    assert "mechababs" in lock.read_text()

    # An environment that was really built, not just declared.
    venv = campaign_mod.venv_path(study, LABEL)
    assert (venv / "bin" / "mechababs").is_file(), (
        "the campaign venv has no mechababs — uv sync did not install the pin")

    # Header only. Which source datasets a campaign acts on is add-dataset's
    # explicit, separate step — never implied by init.
    assert _state_rows(study, LABEL) == [], (
        "campaign init wrote cells; selection belongs to add-dataset")
    assert campaign_mod.state_path(study, LABEL).read_text() == \
        campaign_mod.initial_header()

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
    which = _run(["bash", "-c", f'. "{env_sh}" && echo "$MECHABABS_CAMPAIGN" '
                                f'&& command -v mechababs'], study)
    label, exe = which.stdout.split()
    assert label == LABEL, f"env.sh selected {label!r}, not {LABEL!r}"
    # Resolved on both sides: env.sh derives the venv from its own location with
    # `cd … && pwd`, which resolves symlinks — and a workdir reached through one
    # (a scratch symlink is normal on a cluster) would otherwise fail a string compare.
    expected = (campaign_mod.venv_path(study, LABEL) / "bin" / "mechababs").resolve()
    assert Path(exe).resolve() == expected, (
        f"env.sh activated something other than the campaign venv: {exe}")

    refused = _run(
        [_driver_mechababs(), "add-dataset", "--sourcedata", SOURCEDATA],
        cwd=study,
        env={**os.environ, campaign_mod.CAMPAIGN_ENV_VAR: LABEL},
        check=False,
    )
    assert refused.returncode != 0, (
        "an un-sourced mechababs was allowed to operate on the campaign")
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
            "source_dataset is not the study-relative path the user named")
        assert row["processing_level"] == "subject", row
        assert int(row["n_subjects"]) > 0, "the sniff found no subjects"
        assert row["n_sessions"] == "", (
            "a subject-level dataset reported a session count")
        # Derived columns empty is what makes the next tick scaffold the cell.
        assert row["babs"] == "" and row["merged"] == "", row

    assert anchor["depends_on"] == "", "the anchor app declares no dependency"
    assert chain["depends_on"] == f"{campaign_mod.APPS_DIRNAME}/{ANCHOR}.yaml", (
        "depends_on was not resolved from the declared stem to the producer's "
        f"config path: {chain['depends_on']!r}")

    # The commit is path-scoped to the campaign dir: mechababs' change to a study is
    # additive, so nothing upstream authored is touched by an add.
    touched = _git(study, "show", "--pretty=", "--name-only", "HEAD").split()
    campaign_rel = campaign_mod.campaign_dir(study, LABEL).relative_to(study).as_posix()
    assert touched, "add-dataset committed nothing"
    assert all(p.startswith(campaign_rel) for p in touched), (
        f"add-dataset's commit reaches outside the campaign dir: {touched}")

    _assert_clean(study, "add-dataset")

    # A dataset is selected whole or not at all — the app bundle is fixed at init, so
    # re-adding refuses rather than rewriting or duplicating cells.
    again = _in_campaign(study, LABEL, "add-dataset", "--sourcedata", SOURCEDATA,
                         check=False)
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
    derivative = study / "derivatives" / f"{ANCHOR}+{DATASET_ID}"
    assert derivative.is_dir(), f"no derivative at {derivative}"
    assert (derivative / ".babs").is_dir(), "not a babs project — babs init did not run"
    assert (derivative / "code" / "processing_inclusion.csv").is_file(), (
        "babs recorded no inclusion; --list-sub-file never reached it")

    # Registered as a real subdataset of the study, not a stray directory: that
    # registration is the study's record that this derivative is part of it.
    gitlink = _git(study, "ls-tree", "HEAD", str(derivative.relative_to(study))).split()
    assert gitlink[:2] == ["160000", "commit"], (
        f"the derivative is not registered as a subdataset: {gitlink}")

    # The pin records what was REQUESTED; babs's own processing_inclusion.csv records
    # what it could run. Their diff is what catches a selected subject the data lacks.
    pin = campaign_mod.inclusions_dir(study, LABEL) / \
        f"{SOURCEDATA.replace('/', '-')}_{ANCHOR}.csv"
    assert pin.is_file(), f"no inclusion pinned at {pin}"
    requested = pin.read_text().split()
    assert requested[0] == "sub_id" and len(requested) == 2, (
        f"--limit 1 should pin exactly one subject, got {requested}")

    # The cell's durable fact, and only that cell's.
    rows = {r["app_config"]: r for r in _state_rows(study, LABEL)}
    assert rows[anchor_app]["babs"] == f"derivatives/{ANCHOR}+{DATASET_ID}", rows
    assert rows[anchor_app]["merged"] == "", "scaffold claimed a merge"
    assert rows[f"{campaign_mod.APPS_DIRNAME}/{CHAIN}.yaml"]["babs"] == "", (
        "scaffolding one cell advanced its sibling")

    # The point of the whole chunk: the transition landed as a re-executable
    # command, not as a save with an adjective on it.
    subject = _git(study, "log", "-1", "--format=%s").strip()
    assert subject.startswith("[DATALAD RUNCMD] mechababs scaffold"), subject
    record = _run_record(study)
    assert record["pwd"] == ".", record
    assert record["cmd"] == (
        f"mechababs-inner scaffold --campaign {LABEL} "
        f"--source-dataset {SOURCEDATA} --app {anchor_app}"), record["cmd"]
    assert str(study) not in record["cmd"], (
        "the recorded command carries this machine's path, so it re-executes nowhere")

    # Declared outputs, so this also says nothing undeclared was swept in.
    assert set(record["outputs"]) == {
        f"derivatives/{ANCHOR}+{DATASET_ID}",
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
    assert not (study / "derivatives" / f"{CHAIN}+{DATASET_ID}").exists()
    _assert_clean(study, "the refused dependent cell")



def test_spine(study, cluster_config, app_configs, mechababs_pin, babs_pin,
               simbids_sif):
    """The whole spine, in order. Add later chunks' stages to the bottom.

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


def test_campaign_init_refuses_outside_a_study(tmp_path, cluster_config, app_configs,
                                               mechababs_pin):
    """mechababs operates on a study that already exists, and never authors one.

    The cheapest possible end-to-end proof of that boundary: point `campaign init` at
    a plain directory and it must refuse, rather than helpfully making it a dataset.
    """
    proc = _run([
        _driver_mechababs(), "campaign", "init", LABEL,
        "--apps", str(app_configs / f"{ANCHOR}.yaml"),
        "--cluster", str(cluster_config),
        "--mechababs", mechababs_pin,
    ], cwd=tmp_path, check=False)
    assert proc.returncode != 0, "campaign init created a campaign outside a study"
    assert "not a study" in proc.stderr, proc.stderr
    assert not (tmp_path / campaign_mod.MECHABABS_DIR).exists()


@pytest.fixture(autouse=True)
def _log_phase(request):
    log.info("=== %s ===", request.node.name)
