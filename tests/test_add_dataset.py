"""Unit tests for `mechababs add-dataset --sourcedata <path>`.

The fixture study is a tiny hand-built tree — no datalad, no network, no cluster —
because everything this verb decides is decided from files: which study encloses the
path, what the metadata says about the source dataset, and what is already in the
shard. The one step that reaches outside the process (the `datalad save`) is stubbed;
that it is *asked for*, path-scoped, is asserted.
"""

from contextlib import contextmanager

import pytest
import yaml

from mechababs import add_dataset
from mechababs import campaign as campaign_mod

SUBJECTS_TSV = (
    "source_id\tsubject_id\tsession_id\tdatatypes\tt1w_num\tbold_num\n"
    "ds000001\tsub-01\tn/a\tanat,func\t1\t3\n"
    "ds000001\tsub-02\tn/a\tanat,func\t1\t3\n"
    "ds000002\tsub-01\tses-01\tanat,func\t1\t2\n"
    "ds000002\tsub-01\tses-02\tanat,func\t1\t2\n"
)

APPS = {
    "MRIQC-24.0.2.yaml": "bids_app_args: {}\n",
    "fMRIPrep-25.2.5+anat.yaml": "bids_app_args: {}\n",
    "fMRIPrep-25.2.5+minimal.yaml": "mechababs:\n  depends_on: fMRIPrep-25.2.5+anat\n",
}


@pytest.fixture
def study(tmp_path):
    """A study holding two source datasets, with the metadata TSV that describes them."""
    root = tmp_path / "study-ds000001"
    (root / ".datalad").mkdir(parents=True)
    for source in ("ds000001", "ds000002"):
        # a source dataset is itself a datalad subdataset — so the walk up must not
        # elect it as its own study
        (root / "sourcedata" / source / ".datalad").mkdir(parents=True)
    (root / "sourcedata" / "sourcedata+subjects.tsv").write_text(SUBJECTS_TSV)
    return root


@pytest.fixture
def campaign(study, monkeypatch):
    """Campaign 'nprep' in `study`, selected and passing the env guard.

    Built directly rather than through `campaign init`, so a test can shape the
    bundle (and the shard) in ways init would refuse — which is exactly how the
    dangling-`depends_on` case is reachable at all.
    """
    def build(*app_names, rows=None):
        cdir = campaign_mod.campaign_dir(study, "nprep")
        (cdir / campaign_mod.APPS_DIRNAME).mkdir(parents=True)
        for name in app_names:
            (campaign_mod.apps_dir(study, "nprep") / name).write_text(APPS[name])
        campaign_mod.config_path(study, "nprep").write_text(yaml.safe_dump({
            "label": "nprep",
            "apps": [f"{campaign_mod.APPS_DIRNAME}/{n}" for n in app_names],
            "cluster": "clusters/dartmouth.yaml",
            "limit": None,
        }))
        campaign_mod.state_path(study, "nprep").write_text(campaign_mod.initial_header())
        if rows:
            campaign_mod.write_state(study, "nprep", rows)
        campaign_mod.lock_path(study, "nprep").write_text("lock-v1\n")   # uv.lock
        venv = campaign_mod.venv_path(study, "nprep")
        venv.mkdir()
        campaign_mod.write_env_stamp(venv, "nprep", "lock-v1\n")
        monkeypatch.setenv(campaign_mod.CAMPAIGN_ENV_VAR, "nprep")
        monkeypatch.setattr("sys.prefix", str(venv))
        return cdir
    return build


@pytest.fixture
def saves(monkeypatch):
    """Stub the save scope; record what each block committed and its message.

    The fixture studies here are plain directories, not datalad datasets, so the
    real scope (a datalad status + save) is replaced with a null scope that still
    honors the contract: yields a PendingSave, requires a message on exit.
    """
    calls = []

    @contextmanager
    def null_scope(root, path):
        pending = add_dataset.utils.PendingSave()
        yield pending
        assert pending.message, "scope exited with no message set"
        calls.append((root, pending.message, path))

    monkeypatch.setattr(add_dataset.utils, "campaign_save_scope", null_scope)
    return calls


def cells(study):
    return [(r["source_dataset"], r["app_config"])
            for r in campaign_mod.read_state(study, "nprep")]


# --- finding the study ------------------------------------------------------

def test_the_study_is_found_by_walking_up_from_the_sourcedata(study, campaign, saves):
    campaign("MRIQC-24.0.2.yaml")
    add_dataset.add(study / "sourcedata" / "ds000001")
    assert cells(study) == [("sourcedata/ds000001", "MRIQC-24.0.2")]


def test_the_walk_starts_above_the_sourcedata_not_at_it(study):
    # the source dataset is a dataset root of its own; the study is the one ABOVE it
    found, rel = add_dataset.find_study(study / "sourcedata" / "ds000001")
    assert (found, rel) == (study.resolve(), "sourcedata/ds000001")


def test_a_sourcedata_outside_any_study_is_refused(tmp_path):
    loose = tmp_path / "loose" / "ds000001"
    loose.mkdir(parents=True)
    with pytest.raises(SystemExit) as e:
        add_dataset.find_study(loose)
    assert "not inside a study" in str(e.value)


def test_a_sourcedata_that_is_not_there_is_refused(study):
    # add-dataset selects data already present; it never installs any
    with pytest.raises(SystemExit) as e:
        add_dataset.find_study(study / "sourcedata" / "ds999999")
    assert "does not install" in str(e.value)


def test_a_file_is_not_a_source_dataset(study):
    with pytest.raises(SystemExit):
        add_dataset.find_study(study / "sourcedata" / "sourcedata+subjects.tsv")


# --- the sniff --------------------------------------------------------------

def test_identity_columns_come_from_the_studys_metadata(study, campaign, saves):
    campaign("MRIQC-24.0.2.yaml")
    add_dataset.add(study / "sourcedata" / "ds000001")
    row, = campaign_mod.read_state(study, "nprep")
    assert row == {
        "source_dataset": "sourcedata/ds000001", "app_config": "MRIQC-24.0.2",
        "processing_level": "subject", "n_subjects": "2", "n_sessions": "",
        "depends_on": "", "babs": "", "merged": "",
    }


def test_a_session_level_source_dataset_is_recorded_as_such(study, campaign, saves):
    campaign("MRIQC-24.0.2.yaml")
    added = add_dataset.add(study / "sourcedata" / "ds000002")
    assert (added[0]["processing_level"], added[0]["n_subjects"],
            added[0]["n_sessions"]) == ("session", "1", "2")


def test_a_source_dataset_the_metadata_does_not_describe_is_refused(study, campaign,
                                                                    saves):
    campaign("MRIQC-24.0.2.yaml")
    (study / "sourcedata" / "ds000003" / ".datalad").mkdir(parents=True)
    with pytest.raises(SystemExit) as e:
        add_dataset.add(study / "sourcedata" / "ds000003")
    assert "study metadata" in str(e.value)


# --- the cells --------------------------------------------------------------

def test_one_cell_per_app_in_the_bundle_in_bundle_order(study, campaign, saves):
    campaign("MRIQC-24.0.2.yaml", "fMRIPrep-25.2.5+anat.yaml",
             "fMRIPrep-25.2.5+minimal.yaml")
    add_dataset.add(study / "sourcedata" / "ds000001")
    assert cells(study) == [
        ("sourcedata/ds000001", "MRIQC-24.0.2"),
        ("sourcedata/ds000001", "fMRIPrep-25.2.5+anat"),
        ("sourcedata/ds000001", "fMRIPrep-25.2.5+minimal"),
    ]


def test_depends_on_comes_from_the_app_config(study, campaign, saves):
    campaign("fMRIPrep-25.2.5+anat.yaml", "fMRIPrep-25.2.5+minimal.yaml")
    added = add_dataset.add(study / "sourcedata" / "ds000001")
    assert [(r["app_config"], r["depends_on"]) for r in added] == [
        ("fMRIPrep-25.2.5+anat", ""),
        ("fMRIPrep-25.2.5+minimal", "fMRIPrep-25.2.5+anat"),
    ]


def test_adding_the_whole_bundle_satisfies_its_own_dependencies(study, campaign, saves):
    # the producer is not in the shard yet — it is in this same batch
    campaign("fMRIPrep-25.2.5+anat.yaml", "fMRIPrep-25.2.5+minimal.yaml")
    assert len(add_dataset.add(study / "sourcedata" / "ds000001")) == 2


def test_a_dangling_depends_on_is_refused(study, campaign, saves):
    # a bundle holding the dependent but not its producer: the edge could never
    # resolve, so it fails at the moment the cell would be written
    campaign("fMRIPrep-25.2.5+minimal.yaml")
    with pytest.raises(SystemExit) as e:
        add_dataset.add(study / "sourcedata" / "ds000001")
    assert "depends on 'fMRIPrep-25.2.5+anat'" in str(e.value)
    assert campaign_mod.read_state(study, "nprep") == []


def test_a_producer_already_in_the_shard_satisfies_the_edge(study, campaign, saves):
    campaign("fMRIPrep-25.2.5+minimal.yaml", rows=[{
        "source_dataset": "sourcedata/ds000001",
        "app_config": "fMRIPrep-25.2.5+anat",
    }])
    assert len(add_dataset.add(study / "sourcedata" / "ds000001")) == 1


def test_the_producer_must_be_for_the_SAME_source_dataset(study, campaign, saves):
    # a dependency is a shard-local row lookup keyed on (source dataset, app)
    campaign("fMRIPrep-25.2.5+minimal.yaml", rows=[{
        "source_dataset": "sourcedata/ds000002",
        "app_config": "fMRIPrep-25.2.5+anat",
    }])
    with pytest.raises(SystemExit):
        add_dataset.add(study / "sourcedata" / "ds000001")


# --- re-adding --------------------------------------------------------------

def test_re_adding_the_same_dataset_adds_nothing_and_says_so(study, campaign, saves):
    campaign("MRIQC-24.0.2.yaml")
    add_dataset.add(study / "sourcedata" / "ds000001")
    with pytest.raises(SystemExit) as e:
        add_dataset.add(study / "sourcedata" / "ds000001")
    assert "already selected" in str(e.value)
    assert cells(study) == [("sourcedata/ds000001", "MRIQC-24.0.2")]


def test_a_second_source_dataset_gets_its_own_cells(study, campaign, saves):
    campaign("MRIQC-24.0.2.yaml")
    add_dataset.add(study / "sourcedata" / "ds000001")
    add_dataset.add(study / "sourcedata" / "ds000002")
    assert cells(study) == [("sourcedata/ds000001", "MRIQC-24.0.2"),
                            ("sourcedata/ds000002", "MRIQC-24.0.2")]


def test_an_app_added_to_the_bundle_later_fills_in_the_missing_cell(study, campaign,
                                                                   saves):
    # the shard already holds the mriqc cell; only the new app's cell is written, and
    # the existing row is left exactly as it is (it may carry babs/merged state)
    campaign("MRIQC-24.0.2.yaml", "fMRIPrep-25.2.5+anat.yaml", rows=[{
        "source_dataset": "sourcedata/ds000001", "app_config": "MRIQC-24.0.2",
        "babs": "derivatives/MRIQC-24.0.2", "merged": "yes",
    }])
    added = add_dataset.add(study / "sourcedata" / "ds000001")
    assert [r["app_config"] for r in added] == ["fMRIPrep-25.2.5+anat"]
    assert campaign_mod.read_state(study, "nprep")[0]["merged"] == "yes"


# --- the guards and the commit ----------------------------------------------

def test_the_campaign_guard_runs_against_the_enclosing_study(study, campaign, saves,
                                                             monkeypatch):
    campaign("MRIQC-24.0.2.yaml")
    # the venv of some OTHER environment: the env-match guard must refuse
    monkeypatch.setattr("sys.prefix", str(study / "elsewhere"))
    with pytest.raises(SystemExit) as e:
        add_dataset.add(study / "sourcedata" / "ds000001")
    assert "env.sh" in str(e.value)


def test_no_campaign_selected_is_refused(study, campaign, saves, monkeypatch):
    campaign("MRIQC-24.0.2.yaml")
    monkeypatch.delenv(campaign_mod.CAMPAIGN_ENV_VAR)
    with pytest.raises(SystemExit):
        add_dataset.add(study / "sourcedata" / "ds000001")


def test_the_statefile_change_is_committed_path_scoped_to_the_study(study, campaign,
                                                                   saves):
    campaign("MRIQC-24.0.2.yaml")
    add_dataset.add(study / "sourcedata" / "ds000001")
    saved_study, message, path = saves[0]
    assert (saved_study, path) == (study.resolve(),
                                   campaign_mod.state_path(study.resolve(), "nprep"))
    assert "add-dataset sourcedata/ds000001" in message


def test_a_refused_add_commits_nothing(study, campaign, saves):
    campaign("fMRIPrep-25.2.5+minimal.yaml")
    with pytest.raises(SystemExit):
        add_dataset.add(study / "sourcedata" / "ds000001")
    assert saves == []
