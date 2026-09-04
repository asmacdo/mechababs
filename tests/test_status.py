"""The tall render: one row per cell, and the state each one shows.

`babs status` is stubbed — the live counts are the only thing status adds to the
shard, so what is worth asserting is *which* cells it asks about (only the running
ones), what the state column says for each routed state, and that one unreadable cell
does not cost the view of the others.
"""

import subprocess

import pytest
import yaml

from mechababs import campaign as campaign_mod
from mechababs import iterate as iterate_mod
from mechababs import status as status_mod

LABEL = "e2e"
ANCHOR = "bids-app-configs/SimBIDS-0.0.3+anchor.yaml"
CHAIN = "bids-app-configs/SimBIDS-0.0.3+chain.yaml"
SOURCEDATA = "sourcedata/ds999999"
ANCHOR_PROJECT = "derivatives/SimBIDS-0.0.3+anchor+ds999999+e2e"

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


def declare_apps(root, *app_configs):
    """Write the campaign.yaml bundle — the vocabulary `--app` is checked
    against. Fixed at `campaign init` in real life, so a fixture declares it once."""
    campaign_mod.config_path(root, LABEL).write_text(
        yaml.safe_dump({"label": LABEL, "apps": list(app_configs)})
    )


@pytest.fixture
def study(tmp_path):
    study = tmp_path / "study-ds999999"
    campaign_mod.campaign_dir(study, LABEL).mkdir(parents=True)
    campaign_mod.state_path(study, LABEL).write_text(campaign_mod.initial_header())
    declare_apps(study, ANCHOR, CHAIN)
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


# --------------------------------------------------------------------------
# At a superstudy: the same table, wider. The rollup is computed from the member
# shards at the moment you look, so what is worth asserting is that it reads THEM
# (not a cache at the super), that `installed` is an axis of its own, and that an
# uninstalled member costs neither a shard read nor a babs query.


@pytest.fixture
def superstudy(tmp_path, monkeypatch):
    """A super with two members: `study-dsA` installed, `study-dsB` never installed.

    dsB is the case that matters — registered in the catalog, no working tree, so its
    shard cannot be read at all. dsA carries one merged cell and one active one.
    """
    root = tmp_path / "my-super"
    campaign_mod.campaign_dir(root, LABEL).mkdir(parents=True)
    declare_apps(root, ANCHOR, CHAIN)

    installed = root / "study-dsA"
    campaign_mod.campaign_dir(installed, LABEL).mkdir(parents=True)
    (installed / ".datalad").mkdir()
    campaign_mod.state_path(installed, LABEL).write_text(campaign_mod.initial_header())
    campaign_mod.write_state(
        installed,
        LABEL,
        [
            cell(ANCHOR, babs=ANCHOR_PROJECT, merged="yes"),
            cell(CHAIN, babs="derivatives/chain"),
        ],
    )
    # The active cell's derivative is on disk; the merged one's has been offloaded.
    # That asymmetry is the point of the AND-ed column, so the fixture carries it.
    (installed / "derivatives" / "chain" / ".datalad").mkdir(parents=True)

    # dsB is registered and absent: no directory at all, which is what `datalad
    # uninstall` leaves behind once the mount point is gone.
    campaign_mod.write_members(
        root,
        LABEL,
        [
            {
                "study": "study-dsA",
                "source_dataset": SOURCEDATA,
                "lifecycle": "",
            },
            {
                "study": "study-dsB",
                "source_dataset": SOURCEDATA,
                "lifecycle": "",
            },
        ],
    )

    monkeypatch.setattr(
        campaign_mod,
        "require_selected_campaign",
        lambda path=".", **kw: campaign_mod.Selected(
            root, LABEL, campaign_mod.campaign_dir(root, LABEL), root
        ),
    )
    return root


def _rows(capsys, columns):
    """The rendered table parsed back by the header's column positions."""
    out, err = capsys.readouterr()
    lines = out.splitlines()
    starts, pos = [], 0
    for col in columns:
        pos = lines[0].index(col, pos)
        starts.append(pos)
        pos += len(col)
    bounds = list(zip(starts, starts[1:] + [None]))
    rows = [
        {col: line[a:b].strip() for col, (a, b) in zip(columns, bounds)}
        for line in lines[1:]
    ]
    return rows, err


def test_a_superstudy_renders_every_member_in_catalog_order(
    superstudy, queried, capsys
):
    assert status_mod.run_status() == 0

    rows, _ = _rows(capsys, status_mod.SUPER_COLUMNS)
    assert [r["study"] for r in rows] == ["study-dsA", "study-dsA", "study-dsB"]


def test_installed_is_its_own_column_not_a_state(superstudy, queried, capsys):
    """The two axes are independent. The first row is the one that proves it: a cell
    whose work is finished and whose derivative has since been offloaded is `merged`
    AND `no` — folding them into one column would report it as neither. The absent
    member's catalog row carries no lifecycle here, so its cells read `unknown` rather
    than `not started`: finished vs unseen."""
    assert status_mod.run_status() == 0

    rows, _ = _rows(capsys, status_mod.SUPER_COLUMNS)
    assert [(r["installed"], r["state"]) for r in rows] == [
        (status_mod.NO, status_mod.MERGED),
        (status_mod.YES, status_mod.ACTIVE),
        (status_mod.NO, status_mod.UNKNOWN),
    ]


def test_a_cell_with_nothing_scaffolded_is_installed_when_its_study_is(study, queried):
    """There is no derivative to be missing, so the study alone decides."""
    campaign_mod.write_state(study, LABEL, [cell(ANCHOR)])
    (record,) = status_mod.records(study, LABEL)

    assert status_mod.cell_installed(study, record) == status_mod.YES


def test_an_uninstalled_member_costs_no_babs_query(superstudy, queried, capsys):
    """Its shard is not there to read, so there is nothing to ask babs about — which
    is what keeps a whole-superstudy look cheap when most of it is not on disk."""
    assert status_mod.run_status() == 0

    assert queried == [str(superstudy / "study-dsA" / "derivatives/chain")]


def test_the_summary_goes_to_stderr_and_the_table_to_stdout(
    superstudy, queried, capsys
):
    """Data and commentary, split so `status | grep` sees rows and only rows."""
    assert status_mod.run_status() == 0

    out, err = capsys.readouterr()
    assert err.splitlines() == [
        f"campaign {LABEL!r} · superstudy my-super",
        "2 member(s), 1 installed · 3 cell(s): 1 merged, 1 active, 1 unknown",
    ]
    assert out.splitlines()[0].split() == status_mod.SUPER_COLUMNS


def test_study_narrows_to_one_member(superstudy, queried, capsys):
    assert status_mod.run_status(study="study-dsA") == 0

    rows, err = _rows(capsys, status_mod.SUPER_COLUMNS)
    assert {r["study"] for r in rows} == {"study-dsA"}
    assert "1 member(s), 1 installed" in err


def test_study_refuses_a_directory_that_was_never_selected_in(superstudy, queried):
    """Matched against the catalog, not the filesystem — the same rule `iterate` uses, so
    a typo is an error rather than a quietly empty table."""
    with pytest.raises(SystemExit) as excinfo:
        status_mod.run_status(study="study-dsZ")

    assert "not a member" in str(excinfo.value)


# --------------------------------------------------------------------------
# `--app`: narrowing to one app, and refusing a name the campaign
# does not declare. The declaration is the vocabulary, NOT the visible cells —
# which is what makes a typo catchable when nothing is installed to compare to.


def test_app_narrows_to_one_app(study, queried, capsys):
    campaign_mod.write_state(study, LABEL, [cell(ANCHOR), cell(CHAIN)])

    assert status_mod.report(study, LABEL, app="SimBIDS-0.0.3+chain") == 0

    out, _ = capsys.readouterr()
    rows = [line for line in out.splitlines()[1:]]
    assert len(rows) == 1
    assert "SimBIDS-0.0.3+chain" in rows[0]


def test_app_refuses_a_name_the_campaign_does_not_declare(study, queried):
    campaign_mod.write_state(study, LABEL, [cell(ANCHOR), cell(CHAIN)])

    with pytest.raises(SystemExit) as excinfo:
        status_mod.report(study, LABEL, app="SimBIDS-0.0.3+typo")

    assert "not an app in campaign" in str(excinfo.value)
    assert "SimBIDS-0.0.3+anchor, SimBIDS-0.0.3+chain" in str(excinfo.value)


def test_a_typo_is_refused_even_when_no_member_is_installed(superstudy, queried):
    """The case that decides where the vocabulary comes from. With every member
    uninstalled there is not one readable cell to compare a name against, so a filter
    validated against visible apps would accept anything and render a table of
    `unknown` — reporting a typo as "nothing to see"."""
    (superstudy / "study-dsA" / ".datalad").rmdir()

    with pytest.raises(SystemExit) as excinfo:
        status_mod.run_status(app="SimBIDS-0.0.3+typo")

    assert "not an app in campaign" in str(excinfo.value)


def test_an_uninstalled_member_survives_an_app_filter(superstudy, queried, capsys):
    """Its app is unreadable, not absent. Dropping it would assert this app has no
    cell there, which is exactly the claim `unknown` refuses to make."""
    assert status_mod.run_status(app="SimBIDS-0.0.3+anchor") == 0

    rows, _ = _rows(capsys, status_mod.SUPER_COLUMNS)
    assert [(r["study"], r["app"], r["state"]) for r in rows] == [
        ("study-dsA", "SimBIDS-0.0.3+anchor", status_mod.MERGED),
        ("study-dsB", "", status_mod.UNKNOWN),
    ]


def test_declared_app_stems_reads_the_campaigns_own_declaration(study):
    assert campaign_mod.declared_app_stems(study, LABEL) == [
        "SimBIDS-0.0.3+anchor",
        "SimBIDS-0.0.3+chain",
    ]


def test_an_uninstalled_member_reads_its_committed_lifecycle(
    superstudy, queried, capsys
):
    """The case `installed` exists to serve: a member whose work is finished and whose
    content has been offloaded should read as finished. Its shard is unreachable, so
    the catalog is the only thing that can say so — which is what it is committed for."""
    rows = campaign_mod.read_members(superstudy, LABEL)
    for row in rows:
        if row["study"] == "study-dsB":
            row["lifecycle"] = campaign_mod.LIFECYCLE_MERGED
    campaign_mod.write_members(superstudy, LABEL, rows)

    assert status_mod.run_status() == 0

    rows, _ = _rows(capsys, status_mod.SUPER_COLUMNS)
    absent = [r for r in rows if r["study"] == "study-dsB"]
    assert [(r["installed"], r["state"]) for r in absent] == [
        (status_mod.NO, status_mod.MERGED)
    ]


def test_a_registered_member_reads_in_the_tables_own_words(superstudy, queried, capsys):
    """`registered` is the catalog's word for it; the table already has one, and a
    second vocabulary in the `state` column would also slip past SUMMARY_ORDER —
    counted in the total, then dropped from the parts."""
    rows = campaign_mod.read_members(superstudy, LABEL)
    for row in rows:
        if row["study"] == "study-dsB":
            row["lifecycle"] = campaign_mod.LIFECYCLE_REGISTERED
    campaign_mod.write_members(superstudy, LABEL, rows)

    assert status_mod.run_status() == 0

    out, err = capsys.readouterr()
    assert status_mod.NOT_STARTED in out
    assert "1 not started" in err, err
    assert "registered" not in out
