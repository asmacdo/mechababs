"""The tall render: one row per cell, and the state each one shows.

`babs status` is stubbed — the live counts are the only thing status adds to the
shard, so what is worth asserting is *which* cells it asks about (only the running
ones), what the state column says for each routed state, and that one unreadable cell
does not cost the view of the others.
"""

import subprocess

import pytest

from mechababs import campaign as campaign_mod
from mechababs import iterate as iterate_mod
from mechababs import status as status_mod

LABEL = "e2e"
ANCHOR = "bids-app-configs/SimBIDS-0.0.3+anchor.yaml"
CHAIN = "bids-app-configs/SimBIDS-0.0.3+chain.yaml"
SOURCEDATA = "sourcedata/ds999999"
ANCHOR_PROJECT = "derivatives/SimBIDS-0.0.3+anchor+ds999999"

ALL_DONE = {"total": 2, "submitted": 2, "done": 2, "failed": 0}
STILL_RUNNING = {"total": 2, "submitted": 2, "done": 1, "failed": 0}
SOME_FAILED = {"total": 2, "submitted": 2, "done": 1, "failed": 1}

IDENTITY = {"processing_level": "subject", "n_subjects": "2", "n_sessions": ""}


def cell(app_config, *, depends_on="", babs="", merged=""):
    return {
        "source_dataset": SOURCEDATA,
        "app_config": app_config,
        "depends_on": depends_on,
        "babs": babs,
        "merged": merged,
        **IDENTITY,
    }


@pytest.fixture
def study(tmp_path):
    study = tmp_path / "study-ds999999"
    campaign_mod.campaign_dir(study, LABEL).mkdir(parents=True)
    campaign_mod.state_path(study, LABEL).write_text(campaign_mod.initial_header())
    return study


class _Queried(list):
    """The projects `babs status` was asked about, plus what it answers with.

    `counts` is the knob: a dict of counts, or an exception the stub raises (which is
    how the unreadable-cell case is set up).
    """

    counts = None


@pytest.fixture
def queried(monkeypatch):
    """Stub `babs status`, recording which projects were asked about."""
    asked = _Queried()
    asked.counts = dict(ALL_DONE)

    def fake_status(project):
        asked.append(str(project))
        if isinstance(asked.counts, Exception):
            raise asked.counts
        return dict(asked.counts)

    monkeypatch.setattr(status_mod.babs_status, "read_status", fake_status)
    return asked


def _by_app(records):
    return {r["app"]: r for r in records}


def test_every_cell_gets_a_row_in_shard_order(study, queried):
    campaign_mod.write_state(study, LABEL, [cell(ANCHOR), cell(CHAIN)])

    records = status_mod.records(study, LABEL)

    assert [r["app"] for r in records] == [
        "SimBIDS-0.0.3+anchor",
        "SimBIDS-0.0.3+chain",
    ]
    assert records[0]["source_dataset"] == SOURCEDATA
    assert records[0]["level"] == "subject"
    assert records[0]["subjects"] == "2"


def test_a_not_started_cell_says_so_and_costs_no_query(study, queried):
    campaign_mod.write_state(study, LABEL, [cell(ANCHOR)])

    (record,) = status_mod.records(study, LABEL)

    assert record["state"] == status_mod.NOT_STARTED
    assert record["jobs"] == ""
    assert queried == [], "a not-started cell has nothing volatile to ask about"


def test_a_merged_cell_says_so_and_costs_no_query(study, queried):
    campaign_mod.write_state(
        study, LABEL, [cell(ANCHOR, babs=ANCHOR_PROJECT, merged="true")]
    )

    (record,) = status_mod.records(study, LABEL)

    assert record["state"] == status_mod.MERGED
    assert record["derivative"] == ANCHOR_PROJECT
    assert queried == [], "a done cell was queried"


def test_a_waiting_cell_names_the_producer_it_is_waiting_on(study, queried):
    campaign_mod.write_state(
        study, LABEL, [cell(ANCHOR), cell(CHAIN, depends_on=ANCHOR)]
    )

    record = _by_app(status_mod.records(study, LABEL))["SimBIDS-0.0.3+chain"]

    assert record["state"] == "waiting on SimBIDS-0.0.3+anchor"
    assert queried == []


def test_an_active_cell_carries_the_live_counts(study, queried):
    campaign_mod.write_state(study, LABEL, [cell(ANCHOR, babs=ANCHOR_PROJECT)])
    queried.counts = dict(STILL_RUNNING)

    (record,) = status_mod.records(study, LABEL)

    assert record["state"] == status_mod.ACTIVE
    assert record["jobs"] == "2 job(s): 2 submitted, 1 done, 0 failed"
    assert queried == [str(study / ANCHOR_PROJECT)]


def test_a_cell_whose_jobs_failed_is_called_out_not_left_as_active(study, queried):
    """It is the one row on the table that is stuck, so it must not read like the
    others — the same reason the reconciler flags it rather than merging."""
    campaign_mod.write_state(study, LABEL, [cell(ANCHOR, babs=ANCHOR_PROJECT)])
    queried.counts = dict(SOME_FAILED)

    (record,) = status_mod.records(study, LABEL)

    assert record["state"] == status_mod.FAILED
    assert "1 failed" in record["jobs"]


def test_an_unreadable_cell_is_reported_in_place(study, queried):
    """One broken cell must not cost the view of the other nine."""
    campaign_mod.write_state(
        study,
        LABEL,
        [cell(ANCHOR, babs=ANCHOR_PROJECT), cell(CHAIN, merged="true")],
    )
    queried.counts = subprocess.CalledProcessError(1, "babs")

    records = _by_app(status_mod.records(study, LABEL))

    assert records["SimBIDS-0.0.3+anchor"]["jobs"] == status_mod.UNAVAILABLE
    assert records["SimBIDS-0.0.3+anchor"]["state"] == status_mod.ACTIVE
    assert records["SimBIDS-0.0.3+chain"]["state"] == status_mod.MERGED


def test_the_state_column_is_the_reconcilers_own_reading(study, queried):
    """status and iterate must never disagree about a cell, so there is exactly one
    reading of the columns and both call it."""
    rows = [cell(ANCHOR), cell(CHAIN, depends_on=ANCHOR)]
    campaign_mod.write_state(study, LABEL, rows)

    records = status_mod.records(study, LABEL)

    for record, row in zip(records, rows):
        state, detail = iterate_mod.route(rows, row)
        if state == iterate_mod.WAITING:
            assert record["state"] == f"waiting on {detail}"
        else:
            assert record["state"] == status_mod.NOT_STARTED


# --------------------------------------------------------------------------
# The render itself
# --------------------------------------------------------------------------


def test_the_render_is_a_header_plus_one_line_per_cell(study, queried):
    campaign_mod.write_state(
        study, LABEL, [cell(ANCHOR, babs=ANCHOR_PROJECT, merged="true"), cell(CHAIN)]
    )

    text = status_mod.render(status_mod.records(study, LABEL))
    lines = text.splitlines()

    assert lines[0].split() == status_mod.COLUMNS
    assert len(lines) == 3
    assert SOURCEDATA in lines[1] and status_mod.MERGED in lines[1]
    assert status_mod.NOT_STARTED in lines[2]


def test_the_columns_line_up_and_no_line_trails_whitespace():
    data = [
        {"source_dataset": "sourcedata/ds000001", "app": "a", "state": "merged"},
        {"source_dataset": "x", "app": "a-much-longer-name", "state": "not started"},
    ]
    columns = ["source_dataset", "app", "state"]

    lines = status_mod.render(data, columns).splitlines()

    starts = [
        line.index("merged" if "merged" in line else "not started")
        for line in lines[1:]
    ]
    assert len(set(starts)) == 1, f"the state column is ragged: {lines}"
    assert all(line == line.rstrip() for line in lines), lines


def test_an_empty_campaign_renders_nothing_and_says_why(study, queried, capsys):
    """A campaign with no cells is `campaign init` done and `add-dataset` not — a
    normal state, so it is explained rather than rendered as an empty table."""
    assert status_mod.report(study, LABEL) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "add-dataset" in err
