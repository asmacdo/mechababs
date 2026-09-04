"""The job drill-down: which jobs are shown, and the context babs's CSV lacks.

`job_status.csv` is written here in babs's own column set (`babs/status.py`,
`_job_status_to_row`) — including `ses_id` only for a session-level project, which
is why the column is read by name with a default rather than positionally.
"""

import csv
import subprocess

import pytest
import yaml

from mechababs import campaign as campaign_mod
from mechababs import jobs as jobs_mod

LABEL = "e2e"
ANCHOR = "bids-app-configs/SimBIDS-0.0.3+anchor.yaml"
CHAIN = "bids-app-configs/SimBIDS-0.0.3+chain.yaml"
SOURCEDATA = "sourcedata/ds999999"
ANCHOR_PROJECT = "derivatives/SimBIDS-0.0.3+anchor+ds999999+e2e"
CHAIN_PROJECT = "derivatives/SimBIDS-0.0.3+chain+ds999999+e2e"

BABS_COLUMNS = [
    "sub_id",
    "submitted",
    "is_failed",
    "state",
    "time_used",
    "time_limit",
    "nodes",
    "cpus",
    "partition",
    "name",
    "job_id",
    "task_id",
    "has_results",
]


def job(sub, *, job_id="777", task_id="1", state="DONE", failed=False, ses=None):
    return {
        "sub_id": sub,
        "ses_id": ses or "",
        "submitted": "True",
        "is_failed": str(failed),
        "state": state,
        "time_used": "00:08:12",
        "time_limit": "24:00:00",
        "nodes": "1",
        "cpus": "4",
        "partition": "cpu",
        "name": "bid",
        "job_id": job_id,
        "task_id": task_id,
        "has_results": str(not failed),
    }


def write_job_csv(derivative, rows, *, session=False):
    """babs's CSV, written the way babs writes it."""
    columns = BABS_COLUMNS + (["ses_id"] if session else [])
    (derivative / "code").mkdir(parents=True, exist_ok=True)
    with open(derivative / "code" / "job_status.csv", "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


def cell(app_config, *, babs=""):
    return {
        "source_dataset": SOURCEDATA,
        "app_config": app_config,
        "depends_on": "",
        "babs": babs,
        "merged": "",
        "processing_level": "subject",
        "n_subjects": "2",
        "n_sessions": "",
    }


def declare_apps(root):
    campaign_mod.campaign_dir(root, LABEL).mkdir(parents=True, exist_ok=True)
    campaign_mod.config_path(root, LABEL).write_text(
        yaml.safe_dump({"label": LABEL, "apps": [ANCHOR, CHAIN]})
    )


def make_study(root):
    declare_apps(root)
    (root / ".datalad").mkdir(parents=True, exist_ok=True)
    campaign_mod.state_path(root, LABEL).write_text(campaign_mod.initial_header())
    return root


def select(root, monkeypatch):
    monkeypatch.setattr(
        campaign_mod,
        "require_selected_campaign",
        lambda path=".", **kw: campaign_mod.Selected(
            root, LABEL, campaign_mod.campaign_dir(root, LABEL), root
        ),
    )


@pytest.fixture(autouse=True)
def refreshed(monkeypatch):
    """Stub the refresh, recording which projects babs was asked about.

    Autouse because every path through `run_jobs` refreshes by default: unstubbed, the
    tests would shell out to a `babs` that is not there and silently exercise the
    error branch instead of the behaviour they name.
    """
    asked = []

    def fake_status(project):
        asked.append(str(project))
        return {"total": 0, "submitted": 0, "done": 0, "failed": 0}

    monkeypatch.setattr(jobs_mod.babs_status, "read_status", fake_status)
    return asked


@pytest.fixture
def study(tmp_path, monkeypatch):
    """A lone study: the anchor cell has jobs, the chain cell was never submitted."""
    root = make_study(tmp_path / "study-ds999999")
    campaign_mod.write_state(
        root,
        LABEL,
        [cell(ANCHOR, babs=ANCHOR_PROJECT), cell(CHAIN, babs=CHAIN_PROJECT)],
    )
    write_job_csv(
        root / ANCHOR_PROJECT,
        [job("sub-01", task_id="1"), job("sub-02", task_id="2", failed=True)],
    )
    select(root, monkeypatch)
    return root


def rows(capsys, columns):
    out, err = capsys.readouterr()
    lines = out.splitlines()
    if not lines:
        return [], err
    starts, pos = [], 0
    for col in columns:
        pos = lines[0].index(col, pos)
        starts.append(pos)
        pos += len(col)
    bounds = list(zip(starts, starts[1:] + [None]))
    return [
        {col: line[a:b].strip() for col, (a, b) in zip(columns, bounds)}
        for line in lines[1:]
    ], err


# -------------------------------------------------------------------- job_ref


def test_job_ref_is_slurms_own_array_addressing():
    """`<job>_<task>` is what sacct/squeue print and what `sacct -j` accepts, so the
    column is paste-ready rather than two halves to reassemble."""
    assert jobs_mod.job_ref({"job_id": "63661180", "task_id": "3"}) == "63661180_3"


def test_job_ref_of_a_non_array_job_is_just_its_id():
    assert jobs_mod.job_ref({"job_id": "63661180", "task_id": ""}) == "63661180"


def test_job_ref_of_an_unsubmitted_job_is_empty():
    assert jobs_mod.job_ref({"job_id": "", "task_id": ""}) == ""


# -------------------------------------------------------------------- a study


def test_a_cell_with_no_job_status_csv_is_left_out(study, capsys):
    """Nothing was submitted there, so it has no jobs — a placeholder row would be a
    cell table's answer printed in a job table's shape."""
    assert jobs_mod.run_jobs() == 0

    data, _ = rows(capsys, jobs_mod.COLUMNS)
    assert [r["sub_id"] for r in data] == ["sub-01", "sub-02"]
    assert {r["app"] for r in data} == {"SimBIDS-0.0.3+anchor"}


def test_each_row_carries_the_context_babs_csv_lacks(study, capsys):
    """babs's CSV has no dataset or app column and names every job `bid`."""
    assert jobs_mod.run_jobs() == 0

    data, _ = rows(capsys, jobs_mod.COLUMNS)
    assert data[0]["source_dataset"] == SOURCEDATA
    assert data[0]["app"] == "SimBIDS-0.0.3+anchor"
    assert data[0]["job_id"] == "777_1"
    assert data[0]["logs"] == f"{ANCHOR_PROJECT}/logs"


def test_failed_narrows_to_the_jobs_that_ended_without_results(study, capsys):
    assert jobs_mod.run_jobs(failed=True) == 0

    data, err = rows(capsys, jobs_mod.COLUMNS)
    assert [r["sub_id"] for r in data] == ["sub-02"]
    assert "1 failed" in err


def test_study_is_refused_for_a_study_configured_campaign(study):
    with pytest.raises(SystemExit) as excinfo:
        jobs_mod.run_jobs(study="study-dsA")

    assert "no members to select between" in str(excinfo.value)


def test_a_typo_is_refused_against_the_declared_apps(study):
    with pytest.raises(SystemExit) as excinfo:
        jobs_mod.run_jobs(app="SimBIDS-0.0.3+typo")

    assert "not an app in campaign" in str(excinfo.value)


# --------------------------------------------------------------- a superstudy


@pytest.fixture
def superstudy(tmp_path, monkeypatch):
    """`study-dsA` installed with session-level jobs; `study-dsB` never installed."""
    root = tmp_path / "my-super"
    declare_apps(root)

    member = make_study(root / "study-dsA")
    campaign_mod.write_state(member, LABEL, [cell(ANCHOR, babs=ANCHOR_PROJECT)])
    write_job_csv(
        member / ANCHOR_PROJECT,
        [
            job("sub-01", task_id="1", ses="ses-01"),
            job("sub-01", task_id="2", ses="ses-02", failed=True),
        ],
        session=True,
    )
    campaign_mod.campaign_dir(root / "study-dsB", LABEL).mkdir(parents=True)
    campaign_mod.write_members(
        root,
        LABEL,
        [
            {
                "study": "study-dsA",
                "source_dataset": SOURCEDATA,
                "lifecycle": "pending",
            },
            {
                "study": "study-dsB",
                "source_dataset": SOURCEDATA,
                "lifecycle": "pending",
            },
        ],
    )
    select(root, monkeypatch)
    return root


def test_the_log_path_resolves_from_where_you_are_standing(superstudy, capsys):
    """At a super it carries the member prefix, so it can be pasted straight into
    `less` rather than being relative to a directory you are not in."""
    assert jobs_mod.run_jobs() == 0

    data, _ = rows(capsys, jobs_mod.SUPER_COLUMNS)
    assert data[0]["logs"] == f"study-dsA/{ANCHOR_PROJECT}/logs"
    assert data[0]["study"] == "study-dsA"


def test_ses_id_is_filled_for_a_session_level_cell(superstudy, capsys):
    """babs writes the column only for a session-level project, so it is read by name
    with a default — a subject-level row leaves it empty rather than crashing."""
    assert jobs_mod.run_jobs() == 0

    data, _ = rows(capsys, jobs_mod.SUPER_COLUMNS)
    assert [r["ses_id"] for r in data] == ["ses-01", "ses-02"]


def test_an_uninstalled_member_makes_the_total_a_partial_answer(superstudy, capsys):
    """A total that silently omits members reads as complete."""
    assert jobs_mod.run_jobs() == 0

    _, err = rows(capsys, jobs_mod.SUPER_COLUMNS)
    assert "1 member(s) not installed, their jobs unknown" in err


def test_asking_for_an_uninstalled_member_says_unknown_not_nothing_submitted(
    superstudy, capsys
):
    """The two empties look identical from here and only one of them is true; saying
    the wrong one sends you looking in the wrong place."""
    assert jobs_mod.run_jobs(study="study-dsB") == 0

    out, err = capsys.readouterr()
    assert out == ""
    assert "unknown" in err and "not installed" in err
    assert "nothing submitted" not in err


# ------------------------------------------------------------------- refresh


def test_every_scaffolded_cell_is_refreshed_before_it_is_read(study, refreshed):
    """babs's CSV is a cache it recomputes from the scheduler, and reading it as-is
    can be actively wrong — a resubmitted row keeps the prior attempt's is_failed.

    Every cell with a babs project is asked, including one whose CSV is missing:
    `babs init` writes an initial CSV (bootstrap._create_initial_job_status_csv), so a
    missing one means a broken project rather than an unsubmitted cell, and asking is
    what would rebuild it.
    """
    assert jobs_mod.run_jobs() == 0

    assert refreshed == [str(study / ANCHOR_PROJECT), str(study / CHAIN_PROJECT)]


def test_app_narrows_the_refresh_not_just_the_render(study, refreshed):
    """The refresh is a scheduler query per cell, so narrowing has to happen first or
    `--app` would cost the whole campaign to show one app."""
    assert jobs_mod.run_jobs(app="SimBIDS-0.0.3+anchor") == 0

    assert refreshed == [str(study / ANCHOR_PROJECT)]


def test_no_refresh_reads_the_cache_and_says_so(study, refreshed, capsys):
    """Accepting a possibly-wrong row is an explicit choice, and the header admits
    it — a table that might be stale must not look identical to one that is not."""
    assert jobs_mod.run_jobs(refresh_first=False) == 0

    assert refreshed == []
    _, err = rows(capsys, jobs_mod.COLUMNS)
    assert "not refreshed" in err


def test_scoping_narrows_before_the_refresh(superstudy, refreshed):
    """The refresh is a scheduler query per cell, so `--study` on one member must
    cost one member's queries rather than the whole superstudy's."""
    assert jobs_mod.run_jobs(study="study-dsB") == 0

    assert refreshed == [], "an uninstalled member was queried anyway"


def test_a_cell_babs_cannot_answer_about_does_not_stop_the_rest(
    study, monkeypatch, capsys
):
    """Its CSV is then read as it stands — the --no-refresh outcome for that one cell."""
    monkeypatch.setattr(
        jobs_mod.babs_status,
        "read_status",
        lambda project: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "babs")),
    )

    assert jobs_mod.run_jobs() == 0

    data, _ = rows(capsys, jobs_mod.COLUMNS)
    assert [r["sub_id"] for r in data] == ["sub-01", "sub-02"]
