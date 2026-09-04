"""The submit verb: the guards, the babs it runs, and the column it does NOT write.

The `babs submit` shell-out is stubbed — real jobs need a real scheduler, which is
the e2e's. What is mechababs' here is the state routing (which cells submit advances
and which it refuses), the pinned-babs resolution, and the fact that submit-ness
never lands in the statefile.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from mechababs import campaign as campaign_mod
from mechababs import submit as submit_mod

LABEL = "e2e"
ANCHOR = "bids-app-configs/SimBIDS-0.0.3+anchor.yaml"
CHAIN = "bids-app-configs/SimBIDS-0.0.3+chain.yaml"
SOURCEDATA = "sourcedata/ds999999"
PROJECT = "derivatives/SimBIDS-0.0.3+anchor+ds999999+e2e"


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


@pytest.fixture
def babs_calls(monkeypatch):
    """Stub the `babs` shell-out; collect the argv and kwargs it was called with."""
    calls = []
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if Path(str(cmd[0])).name != "babs":
            return real_run(cmd, **kwargs)
        calls.append({"cmd": [str(c) for c in cmd], "kwargs": kwargs})
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(submit_mod.subprocess, "run", fake_run)
    return calls


def _row(study, app_config):
    return next(
        r
        for r in campaign_mod.read_state(study, LABEL)
        if r["app_config"] == app_config
    )


def test_submit_runs_babs_against_the_cells_project(study, babs_calls):
    project = submit_mod.submit(study, LABEL, SOURCEDATA, ANCHOR)

    assert project == PROJECT
    (call,) = babs_calls
    assert call["cmd"][1:] == ["submit", PROJECT], call["cmd"]
    # Study-relative path with the study as cwd: babs absolutizes against cwd, and
    # the verb stays indifferent to where the reconciler is standing.
    assert call["kwargs"]["cwd"] == str(study)


def test_submit_runs_the_pinned_babs_not_paths(study, babs_calls):
    """A stray user-level babs on PATH has shadowed the pinned one before; the
    campaign venv is the pin, so the binary is resolved beside sys.prefix."""
    submit_mod.submit(study, LABEL, SOURCEDATA, ANCHOR)
    (call,) = babs_calls
    exe = Path(call["cmd"][0])
    assert exe.is_absolute() and exe.name == "babs", exe
    assert exe.is_relative_to(sys.prefix), exe


def test_submit_writes_no_column(study, babs_calls):
    """Submitted-ness is volatile and babs owns it — the statefile must not mirror
    it, or there is a cache to drift."""
    before = campaign_mod.state_path(study, LABEL).read_text()
    submit_mod.submit(study, LABEL, SOURCEDATA, ANCHOR)
    assert campaign_mod.state_path(study, LABEL).read_text() == before


def test_an_unscaffolded_cell_is_refused(study, babs_calls):
    """The chain cell has never been scaffolded, so there is no project to submit."""
    with pytest.raises(SystemExit, match="not scaffolded"):
        submit_mod.submit(study, LABEL, SOURCEDATA, CHAIN)
    assert babs_calls == []


def test_a_merged_cell_is_refused(study, babs_calls):
    """The self-guard against a `datalad rerun` (or a hand-run) landing on a cell
    that has since finished: submitting again would deploy jobs into a derivative
    whose results are already consolidated."""
    rows = campaign_mod.read_state(study, LABEL)
    rows[0]["merged"] = "true"
    campaign_mod.write_state(study, LABEL, rows)

    with pytest.raises(SystemExit, match="already merged"):
        submit_mod.submit(study, LABEL, SOURCEDATA, ANCHOR)
    assert babs_calls == []


def test_a_cell_that_is_not_in_the_shard_is_refused(study, babs_calls):
    with pytest.raises(SystemExit, match="no cell for"):
        submit_mod.submit(study, LABEL, "sourcedata/nope", ANCHOR)
    assert babs_calls == []


def test_a_failing_babs_submit_propagates(study, monkeypatch):
    """`check=True`, so a scheduler refusal is an error the reconciler sees — not a
    silent iterate that reports the cell advanced."""

    def boom(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(submit_mod.subprocess, "run", boom)
    with pytest.raises(subprocess.CalledProcessError):
        submit_mod.submit(study, LABEL, SOURCEDATA, ANCHOR)
