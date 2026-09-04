"""The merge verb: the guards, the two-step consolidation, and the column it writes.

`babs merge` and `datalad update` are stubbed — consolidating real result branches
needs real jobs, which is the e2e's. What is mechababs' here is the state routing,
the live-counts recheck that refuses a premature merge, the order of the two steps,
and the `merged` column landing on exactly one cell.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mechababs import campaign as campaign_mod
from mechababs import merge as merge_mod

LABEL = "e2e"
ANCHOR = "bids-app-configs/SimBIDS-0.0.3+anchor.yaml"
CHAIN = "bids-app-configs/SimBIDS-0.0.3+chain.yaml"
SOURCEDATA = "sourcedata/ds999999"
PROJECT = "derivatives/SimBIDS-0.0.3+anchor+ds999999+e2e"

ALL_DONE = {"total": 2, "submitted": 2, "done": 2, "failed": 0}
STILL_RUNNING = {"total": 2, "submitted": 2, "done": 1, "failed": 0}
SOME_FAILED = {"total": 2, "submitted": 2, "done": 1, "failed": 1}
UNSUBMITTED = {"total": 2, "submitted": 0, "done": 0, "failed": 0}


@pytest.fixture
def study(tmp_path):
    """A study whose anchor cell is scaffolded and unmerged — the ACTIVE state."""
    study = tmp_path / "study-ds999999"
    campaign_mod.campaign_dir(study, LABEL).mkdir(parents=True)
    campaign_mod.state_path(study, LABEL).write_text(campaign_mod.initial_header())
    identity = {"processing_level": "subject", "n_subjects": "2", "n_sessions": ""}
    campaign_mod.write_state(
        study,
        LABEL,
        [
            {
                "source_dataset": SOURCEDATA,
                "app_config": ANCHOR,
                "depends_on": "",
                "babs": PROJECT,
                **identity,
            },
            {
                "source_dataset": SOURCEDATA,
                "app_config": CHAIN,
                "depends_on": ANCHOR,
                **identity,
            },
        ],
    )
    return study


class _Steps(list):
    """The recorded steps, plus the babs status the stub reports.

    A list so a test can say `steps == []`; the `status` attribute is the knob that
    decides whether the verb's own recheck lets the merge proceed.
    """

    status = None


@pytest.fixture
def steps(monkeypatch):
    """Stub every shell-out merge makes, and record them in order."""
    calls = _Steps()
    calls.status = dict(ALL_DONE)
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if Path(str(cmd[0])).name != "babs":
            return real_run(cmd, **kwargs)
        if cmd[1] == "status":
            calls.append({"step": "status", "cmd": [str(c) for c in cmd]})
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(calls.status))
        calls.append({"step": "babs", "cmd": [str(c) for c in cmd], "kwargs": kwargs})
        return subprocess.CompletedProcess(cmd, 0)

    def fake_update(**kwargs):
        calls.append({"step": "update", "kwargs": kwargs})
        return [{"status": "ok", "path": kwargs["dataset"]}]

    # One patch covers both callers: `merge` and `babs_status` name the same
    # `subprocess` module object. Only `babs` is intercepted, so the git shell-outs
    # underneath still really run.
    monkeypatch.setattr(merge_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(merge_mod.datalad_api, "update", fake_update)
    return calls


def _row(study, app_config):
    return next(
        r
        for r in campaign_mod.read_state(study, LABEL)
        if r["app_config"] == app_config
    )


def test_merge_consolidates_then_pulls_then_records(study, steps):
    """The order is the point: babs consolidates in the RIA, the derivative is
    fast-forwarded onto that, and only then is the cell called done."""
    project = merge_mod.merge(study, LABEL, SOURCEDATA, ANCHOR)

    assert project == PROJECT
    assert [c["step"] for c in steps] == ["status", "babs", "update"]

    babs = next(c for c in steps if c["step"] == "babs")
    assert babs["cmd"][1:] == ["merge", PROJECT], babs["cmd"]
    assert babs["kwargs"]["cwd"] == str(study)
    exe = Path(babs["cmd"][0])
    assert exe.is_absolute() and exe.is_relative_to(sys.prefix), exe

    update = next(c for c in steps if c["step"] == "update")
    assert update["kwargs"]["dataset"] == str(study / PROJECT)
    assert update["kwargs"]["sibling"] == merge_mod.OUTPUT_SIBLING
    assert update["kwargs"]["how"] == "merge"

    assert _row(study, ANCHOR)["merged"] == "true"
    assert _row(study, CHAIN)["merged"] == "", "merging one cell advanced its sibling"


@pytest.mark.parametrize(
    "status, decision",
    [(STILL_RUNNING, "skip"), (SOME_FAILED, "fail"), (UNSUBMITTED, "submit")],
)
def test_a_cell_that_is_not_ready_is_refused(study, steps, status, decision):
    """The recheck, and the reason it exists: babs merges whatever result branches
    it finds, so a premature merge is silent rather than loud."""
    steps.status = status

    with pytest.raises(SystemExit, match=f"not ready to merge: babs says '{decision}'"):
        merge_mod.merge(study, LABEL, SOURCEDATA, ANCHOR)

    assert [c["step"] for c in steps] == ["status"], "it got past the recheck"
    assert _row(study, ANCHOR)["merged"] == ""


def test_an_unscaffolded_cell_is_refused_before_babs_is_asked(study, steps):
    """Cheap guard first: there is no project to run `babs status` against."""
    with pytest.raises(SystemExit, match="not scaffolded"):
        merge_mod.merge(study, LABEL, SOURCEDATA, CHAIN)
    assert steps == []


def test_an_already_merged_cell_is_refused(study, steps):
    """The self-guard a `datalad rerun` onto current HEAD lands on."""
    rows = campaign_mod.read_state(study, LABEL)
    rows[0]["merged"] = "true"
    campaign_mod.write_state(study, LABEL, rows)

    with pytest.raises(SystemExit, match="already merged"):
        merge_mod.merge(study, LABEL, SOURCEDATA, ANCHOR)
    assert steps == []


def test_a_cell_that_is_not_in_the_shard_is_refused(study, steps):
    with pytest.raises(SystemExit, match="no cell for"):
        merge_mod.merge(study, LABEL, "sourcedata/nope", ANCHOR)
    assert steps == []


def test_a_failed_pull_is_loud_and_leaves_the_cell_unmerged(study, steps, monkeypatch):
    """The results are consolidated in the RIA at this point but the derivative does
    not carry them, so the cell is NOT done — and must not be recorded as done."""

    def failing_update(**kwargs):
        return [{"status": "error", "path": kwargs["dataset"]}]

    monkeypatch.setattr(merge_mod.datalad_api, "update", failing_update)
    with pytest.raises(SystemExit, match="could not merge"):
        merge_mod.merge(study, LABEL, SOURCEDATA, ANCHOR)
    assert _row(study, ANCHOR)["merged"] == ""


def test_a_failing_babs_merge_propagates(study, steps, monkeypatch):
    """`check=True`, so a merge that blew up is an error the reconciler sees — and
    the cell stays unmerged, to be looked at rather than skipped past."""
    stubbed = merge_mod.subprocess.run

    def boom(cmd, **kwargs):
        if Path(str(cmd[0])).name == "babs" and cmd[1] == "merge":
            raise subprocess.CalledProcessError(1, cmd)
        return stubbed(cmd, **kwargs)

    monkeypatch.setattr(merge_mod.subprocess, "run", boom)
    with pytest.raises(subprocess.CalledProcessError):
        merge_mod.merge(study, LABEL, SOURCEDATA, ANCHOR)
    assert _row(study, ANCHOR)["merged"] == ""
