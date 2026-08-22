"""Unit tests for the babs status --json decision seam (mechababs/babs_status.py).

Pure functions — no cluster, no babs. `decide` maps the `--json` counts to the
one next transition; `read_status` just runs the command and json.loads it.
"""

import json
import sys
from pathlib import Path

import pytest

from mechababs import babs_status
from mechababs.babs_status import decide


def _status(**over):
    """A valid all-done 5/5 baseline (the ds005896 mriqc example), with overrides.
    Mirrors the full `babs status --json` object; decide reads only four keys."""
    base = dict(
        total=5,
        submitted=5,
        unsubmitted=0,
        pending=0,
        running=0,
        completing=0,
        configuring=0,
        done=5,
        failed=0,
    )
    base.update(over)
    return base


@pytest.mark.parametrize(
    "status, expected",
    [
        # not all submitted -> deploy the rest (even if some already running)
        (_status(submitted=2, unsubmitted=3, done=2), "submit"),
        (_status(submitted=2, unsubmitted=3, running=1, done=1), "submit"),
        # all submitted, some still in flight -> wait
        (_status(running=3, done=2), "skip"),
        # a completing job (no results) is in-progress via submitted-done-failed
        (_status(completing=1, done=4), "skip"),
        # all ended, some failed -> flag, don't merge
        (_status(done=3, failed=2), "fail"),
        (_status(done=0, failed=5), "fail"),
        # all ended, all succeeded -> merge
        (_status(), "merge"),
    ],
)
def test_decide(status, expected):
    assert decide(status) == expected


def test_decide_submit_precedes_fail():
    # A failure among ended jobs does NOT short-circuit deploying the rest.
    assert decide(_status(submitted=3, unsubmitted=2, done=2, failed=1)) == "submit"


def test_decide_tolerates_substate_results_overlap():
    # Raw scheduler snapshot can double-count: a job that pushed results while
    # still COMPLETING shows in both `done` and `completing`. decide derives
    # in_progress from submitted-done-failed, so it correctly merges.
    assert decide(_status(total=1, submitted=1, done=1, completing=1)) == "merge"


def test_decide_needs_only_four_keys():
    # decide ignores the live-state buckets entirely — works without them.
    assert decide({"total": 5, "submitted": 5, "done": 5, "failed": 0}) == "merge"


def test_read_status_parses_stdout(monkeypatch):
    from types import SimpleNamespace

    stdout = json.dumps(_status())

    def fake_run(cmd, **kw):
        # The PINNED babs, not PATH's: this query is what the reconciler routes on
        # and what the merge verb re-checks, so it has to be the babs that ran the
        # jobs. Asserted by shape (an absolute path under sys.prefix), not by the
        # literal string, which is environment-dependent.
        assert Path(cmd[0]).name == "babs" and Path(cmd[0]).is_absolute(), cmd
        assert Path(cmd[0]).is_relative_to(sys.prefix), cmd
        assert cmd[1:4] == ["status", "--json", "some/project"]
        return SimpleNamespace(stdout=stdout)

    monkeypatch.setattr(babs_status.subprocess, "run", fake_run)
    assert babs_status.read_status("some/project") == _status()
