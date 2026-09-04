"""Unit tests for `mechababs add-dataset --sourcedata <path>`.

The fixture study is a tiny hand-built tree — no datalad, no network, no cluster —
because everything this verb decides is decided from files: which study encloses the
path, what the metadata says about the source dataset, and what is already in the
shard. The one step that reaches outside the process (the `datalad save`) is stubbed;
that it is *asked for*, path-scoped, is asserted.
"""

import importlib
from contextlib import contextmanager

import pytest
import yaml

from mechababs import add_dataset
from conftest import pretend_uv_check, stamp_dataset_id
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
        campaign_mod.config_path(study, "nprep").write_text(
            yaml.safe_dump(
                {
                    "label": "nprep",
                    "apps": [f"{campaign_mod.APPS_DIRNAME}/{n}" for n in app_names],
                    "cluster": "clusters/dartmouth.yaml",
                    "limit": None,
                }
            )
        )
        campaign_mod.state_path(study, "nprep").write_text(
            campaign_mod.initial_header()
        )
        if rows:
            campaign_mod.write_state(study, "nprep", rows)
        campaign_mod.uv_lock_path(study, "nprep").write_text("lock-v1\n")  # uv.lock
        venv = campaign_mod.venv_path(study, "nprep")
        venv.mkdir()
        pretend_uv_check(monkeypatch)
        monkeypatch.setenv(campaign_mod.CAMPAIGN_ENV_VAR, "nprep")
        monkeypatch.setattr("sys.prefix", str(venv))
        monkeypatch.chdir(study)  # operating verbs run from the campaign root
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
    return [
        (r["source_dataset"], r["app_config"])
        for r in campaign_mod.read_state(study, "nprep")
    ]


# --- resolving the sourcedata (from the study root) -------------------------


def test_sourcedata_is_taken_relative_to_the_study_root(study, campaign, saves):
    campaign("MRIQC-24.0.2.yaml")
    add_dataset.add("sourcedata/ds000001")  # relative, exactly as the user types it
    assert cells(study) == [
        ("sourcedata/ds000001", "bids-app-configs/MRIQC-24.0.2.yaml")
    ]


def test_an_absolute_path_inside_the_study_also_works(study, campaign, saves):
    campaign("MRIQC-24.0.2.yaml")
    add_dataset.add(study / "sourcedata" / "ds000001")
    assert cells(study) == [
        ("sourcedata/ds000001", "bids-app-configs/MRIQC-24.0.2.yaml")
    ]


def test_a_path_outside_the_study_is_refused(study, tmp_path):
    loose = tmp_path / "loose" / "ds000001"
    loose.mkdir(parents=True)
    with pytest.raises(SystemExit) as e:
        add_dataset.resolve_sourcedata(study, loose)
    assert "not inside this study" in str(e.value)


def test_a_sourcedata_that_is_not_there_is_refused(study):
    # add-dataset selects data already present; it never installs any
    with pytest.raises(SystemExit) as e:
        add_dataset.resolve_sourcedata(study, "sourcedata/ds999999")
    assert "does not install" in str(e.value)


def test_a_file_is_not_a_source_dataset(study):
    with pytest.raises(SystemExit):
        add_dataset.resolve_sourcedata(study, "sourcedata/sourcedata+subjects.tsv")


# --- the sniff --------------------------------------------------------------


def test_identity_columns_come_from_the_studys_metadata(study, campaign, saves):
    campaign("MRIQC-24.0.2.yaml")
    add_dataset.add(study / "sourcedata" / "ds000001")
    (row,) = campaign_mod.read_state(study, "nprep")
    assert row == {
        "source_dataset": "sourcedata/ds000001",
        "app_config": "bids-app-configs/MRIQC-24.0.2.yaml",
        "processing_level": "subject",
        "n_subjects": "2",
        "n_sessions": "",
        "depends_on": "",
        "babs": "",
        "merged": "",
    }


def test_a_session_level_source_dataset_is_recorded_as_such(study, campaign, saves):
    campaign("MRIQC-24.0.2.yaml")
    added = add_dataset.add(study / "sourcedata" / "ds000002")
    assert (
        added[0]["processing_level"],
        added[0]["n_subjects"],
        added[0]["n_sessions"],
    ) == ("session", "1", "2")


def test_a_source_dataset_the_metadata_does_not_describe_is_refused(
    study, campaign, saves
):
    campaign("MRIQC-24.0.2.yaml")
    (study / "sourcedata" / "ds000003" / ".datalad").mkdir(parents=True)
    with pytest.raises(SystemExit) as e:
        add_dataset.add(study / "sourcedata" / "ds000003")
    assert "study metadata" in str(e.value)


# --- the cells --------------------------------------------------------------


def test_one_cell_per_app_in_the_bundle_in_bundle_order(study, campaign, saves):
    campaign(
        "MRIQC-24.0.2.yaml", "fMRIPrep-25.2.5+anat.yaml", "fMRIPrep-25.2.5+minimal.yaml"
    )
    add_dataset.add(study / "sourcedata" / "ds000001")
    assert cells(study) == [
        ("sourcedata/ds000001", "bids-app-configs/MRIQC-24.0.2.yaml"),
        ("sourcedata/ds000001", "bids-app-configs/fMRIPrep-25.2.5+anat.yaml"),
        ("sourcedata/ds000001", "bids-app-configs/fMRIPrep-25.2.5+minimal.yaml"),
    ]


def test_depends_on_comes_from_the_app_config(study, campaign, saves):
    campaign("fMRIPrep-25.2.5+anat.yaml", "fMRIPrep-25.2.5+minimal.yaml")
    added = add_dataset.add(study / "sourcedata" / "ds000001")
    assert [(r["app_config"], r["depends_on"]) for r in added] == [
        ("bids-app-configs/fMRIPrep-25.2.5+anat.yaml", ""),
        (
            "bids-app-configs/fMRIPrep-25.2.5+minimal.yaml",
            "bids-app-configs/fMRIPrep-25.2.5+anat.yaml",
        ),
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


def test_another_datasets_producer_row_does_not_satisfy_the_edge(
    study, campaign, saves
):
    # the edge is per source dataset; ds000002's anat cell says nothing about ds000001
    campaign(
        "fMRIPrep-25.2.5+minimal.yaml",
        rows=[
            {
                "source_dataset": "sourcedata/ds000002",
                "app_config": "bids-app-configs/fMRIPrep-25.2.5+anat.yaml",
            }
        ],
    )
    with pytest.raises(SystemExit):
        add_dataset.add(study / "sourcedata" / "ds000001")


# --- re-adding --------------------------------------------------------------


def test_re_adding_the_same_dataset_adds_nothing_and_says_so(study, campaign, saves):
    campaign("MRIQC-24.0.2.yaml")
    add_dataset.add(study / "sourcedata" / "ds000001")
    with pytest.raises(SystemExit) as e:
        add_dataset.add(study / "sourcedata" / "ds000001")
    assert "already selected" in str(e.value)
    assert cells(study) == [
        ("sourcedata/ds000001", "bids-app-configs/MRIQC-24.0.2.yaml")
    ]


def test_a_second_source_dataset_gets_its_own_cells(study, campaign, saves):
    campaign("MRIQC-24.0.2.yaml")
    add_dataset.add(study / "sourcedata" / "ds000001")
    add_dataset.add(study / "sourcedata" / "ds000002")
    assert cells(study) == [
        ("sourcedata/ds000001", "bids-app-configs/MRIQC-24.0.2.yaml"),
        ("sourcedata/ds000002", "bids-app-configs/MRIQC-24.0.2.yaml"),
    ]


def test_bundle_growth_is_unsupported_a_partial_dataset_still_refuses(
    study, campaign, saves
):
    # the bundle is fixed at init (growth deliberately unsupported — #116): a dataset
    # with ANY cell refuses whole, and its existing state is left exactly as it is
    campaign(
        "MRIQC-24.0.2.yaml",
        "fMRIPrep-25.2.5+anat.yaml",
        rows=[
            {
                "source_dataset": "sourcedata/ds000001",
                "app_config": "bids-app-configs/MRIQC-24.0.2.yaml",
                "babs": "derivatives/MRIQC-24.0.2",
                "merged": "yes",
            }
        ],
    )
    with pytest.raises(SystemExit) as e:
        add_dataset.add(study / "sourcedata" / "ds000001")
    assert "new campaign" in str(e.value)
    assert campaign_mod.read_state(study, "nprep")[0]["merged"] == "yes"


# --- the guards and the commit ----------------------------------------------


def test_the_campaign_guard_runs_against_the_enclosing_study(
    study, campaign, saves, monkeypatch
):
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


def test_the_statefile_change_is_committed_path_scoped_to_the_study(
    study, campaign, saves
):
    campaign("MRIQC-24.0.2.yaml")
    add_dataset.add(study / "sourcedata" / "ds000001")
    saved_study, message, path = saves[0]
    assert (saved_study, path) == (
        study.resolve(),
        campaign_mod.state_path(study.resolve(), "nprep"),
    )
    assert "add-dataset sourcedata/ds000001" in message


def test_a_refused_add_commits_nothing(study, campaign, saves):
    campaign("fMRIPrep-25.2.5+minimal.yaml")
    with pytest.raises(SystemExit):
        add_dataset.add(study / "sourcedata" / "ds000001")
    assert saves == []


# --- at a superstudy: reaching a member -------------------------------------
#
# The verb still runs from the campaign root, which at a super is the superstudy.
# Reaching a member takes a second coordinate (--study), never a different place
# to stand — that is what keeps "operate a campaign only from the level it was
# configured" true of add-dataset as well as of iterate.


@pytest.fixture
def superstudy(tmp_path, monkeypatch):
    """A superstudy with campaign 'nprep' configured at it, and one member study."""
    root = tmp_path / "my-super"
    (root / ".datalad").mkdir(parents=True)
    # A superstudy needs a datalad-id: it is what a member's marker records, so that
    # the relationship survives the member being cloned somewhere else.
    stamp_dataset_id(root)
    cdir = campaign_mod.campaign_dir(root, "nprep")
    (cdir / campaign_mod.APPS_DIRNAME).mkdir(parents=True)
    (campaign_mod.apps_dir(root, "nprep") / "MRIQC-24.0.2.yaml").write_text(
        APPS["MRIQC-24.0.2.yaml"]
    )
    campaign_mod.config_path(root, "nprep").write_text(
        yaml.safe_dump(
            {
                "label": "nprep",
                "apps": [f"{campaign_mod.APPS_DIRNAME}/MRIQC-24.0.2.yaml"],
                "cluster": "clusters/dartmouth.yaml",
                "limit": None,
            }
        )
    )
    campaign_mod.members_path(root, "nprep").write_text(
        campaign_mod.initial_members_header()
    )
    campaign_mod.uv_lock_path(root, "nprep").write_text("lock-v1\n")
    venv = campaign_mod.venv_path(root, "nprep")
    venv.mkdir()
    pretend_uv_check(monkeypatch)

    member = root / "study-ds000001"
    (member / ".datalad").mkdir(parents=True)
    (member / "sourcedata" / "ds000001" / ".datalad").mkdir(parents=True)
    (member / "sourcedata" / "sourcedata+subjects.tsv").write_text(SUBJECTS_TSV)

    monkeypatch.setenv(campaign_mod.CAMPAIGN_ENV_VAR, "nprep")
    monkeypatch.setattr("sys.prefix", str(venv))
    monkeypatch.chdir(root)
    return root, member


def test_the_member_gains_the_campaign_footprint_on_first_selection(superstudy, saves):
    """campaign init at a super fans out to nothing — no members are chosen yet — so
    a member receives the campaign at the moment it is first selected into it."""
    root, member = superstudy

    add_dataset.add("sourcedata/ds000001", "study-ds000001")

    cdir = campaign_mod.campaign_dir(member, "nprep")
    assert (cdir / campaign_mod.STATE_FILENAME).is_file()
    assert (cdir / campaign_mod.APPS_DIRNAME / "MRIQC-24.0.2.yaml").is_file()
    assert (cdir / campaign_mod.UV_LOCK_FILENAME).read_text() == "lock-v1\n"
    # the operational venv lives at the configured level; a member is not operated from
    assert not (cdir / campaign_mod.ENV_FILENAME).exists()


def test_the_member_is_marked_as_belonging_to_the_superstudy(superstudy, saves):
    root, member = superstudy

    add_dataset.add("sourcedata/ds000001", "study-ds000001")

    assert campaign_mod.superstudy_of(member, "nprep") == root.resolve()


def test_the_cells_land_in_the_members_shard_not_the_super(superstudy, saves):
    """Per-cell state shards to the members; the super carries membership only."""
    root, member = superstudy

    add_dataset.add("sourcedata/ds000001", "study-ds000001")

    assert [r["source_dataset"] for r in campaign_mod.read_state(member, "nprep")] == [
        "sourcedata/ds000001"
    ]
    assert not campaign_mod.state_path(root, "nprep").exists()


def test_the_super_records_the_membership_row(superstudy, saves):
    root, member = superstudy

    add_dataset.add("sourcedata/ds000001", "study-ds000001")

    assert campaign_mod.read_members(root, "nprep") == [
        {
            "study": "study-ds000001",
            "source_dataset": "sourcedata/ds000001",
            "lifecycle": campaign_mod.LIFECYCLE_REGISTERED,
        }
    ]


def test_member_and_super_are_committed_separately(superstudy, saves):
    """Different datasets, so each records its own change where a reader of that
    dataset alone will find it — and the member is saved first, so the gitlink the
    super registers already points at the state its catalog row describes."""
    root, member = superstudy

    add_dataset.add("sourcedata/ds000001", "study-ds000001")

    roots = [call[0] for call in saves]
    assert roots == [member, root]


def test_sourcedata_is_relative_to_the_member_not_the_super(superstudy, saves):
    root, member = superstudy
    # the same relative path exists at the super, and must NOT be what gets selected
    (root / "sourcedata" / "ds000001").mkdir(parents=True)

    added = add_dataset.add("sourcedata/ds000001", "study-ds000001")

    assert added[0]["source_dataset"] == "sourcedata/ds000001"
    assert campaign_mod.read_state(member, "nprep")


# --- the configured-level rule, enforced in both directions -----------------


def test_a_super_campaign_refuses_add_dataset_without_a_member(superstudy, saves):
    with pytest.raises(SystemExit) as excinfo:
        add_dataset.add("sourcedata/ds000001")
    assert "--study <member>" in str(excinfo.value)


def test_a_study_campaign_refuses_a_member_argument(study, campaign, saves):
    campaign("MRIQC-24.0.2.yaml")
    with pytest.raises(SystemExit) as excinfo:
        add_dataset.add("sourcedata/ds000001", "some-member")
    assert "no member to name" in str(excinfo.value)


def test_a_member_refuses_add_dataset_and_points_at_its_super(superstudy, saves):
    """The reverse direction: standing in a member of a super-campaign, the verb
    refuses and names the directory to run from."""
    root, member = superstudy
    add_dataset.add("sourcedata/ds000001", "study-ds000001")

    import os

    os.chdir(member)
    with pytest.raises(SystemExit) as excinfo:
        add_dataset.add("sourcedata/ds000002")
    assert "operated from its superstudy" in str(excinfo.value)
    assert "carries no environment of its own" in str(excinfo.value)


def test_a_member_outside_the_superstudy_is_refused(superstudy, saves, tmp_path):
    outside = tmp_path / "elsewhere"
    (outside / ".datalad").mkdir(parents=True)
    with pytest.raises(SystemExit) as excinfo:
        add_dataset.add("sourcedata/ds000001", str(outside))
    assert "not inside this superstudy" in str(excinfo.value)


def test_a_member_that_is_not_a_study_is_refused(superstudy, saves):
    root, _ = superstudy
    (root / "not-a-study").mkdir()
    with pytest.raises(SystemExit) as excinfo:
        add_dataset.add("sourcedata/ds000001", "not-a-study")
    assert "not a member study" in str(excinfo.value)


def test_a_registered_but_uninstalled_member_is_named_as_such(superstudy, saves):
    """After a plain clone of a real superstudy the members are registered in
    `.gitmodules` but not installed, and the fix is `datalad get -n`, not a re-clone."""
    root, _ = superstudy
    (root / ".gitmodules").write_text(
        '[submodule "study-ds000009"]\n\tpath = study-ds000009\n\turl = ./x\n'
    )
    (root / "study-ds000009").mkdir()  # an empty mount point, as git leaves it
    with pytest.raises(SystemExit) as excinfo:
        add_dataset.add("sourcedata/ds000001", "study-ds000009")
    message = str(excinfo.value)
    assert "not installed" in message
    assert "datalad get -n study-ds000009" in message
    assert "not a member study" not in message


# --- --study as a URL -------------------------------------------------------


@pytest.mark.parametrize(
    "arg,is_url",
    [
        ("https://github.com/OpenNeuroStudies/study-ds000001", True),
        ("git@github.com:OpenNeuroStudies/study-ds000001.git", True),
        ("study-ds000001", False),
        ("./study-ds000001", False),
        ("/abs/study-ds000001", False),
    ],
)
def test_url_detection(arg, is_url):
    assert bool(add_dataset.looks_like_url(arg)) is is_url


def test_a_url_member_is_cloned_in_then_selected(superstudy, saves, monkeypatch):
    """The one case where a selection verb brings something in: a member study is
    the container for source data, not the data. Source content is still not fetched."""
    root, member = superstudy
    cloned = {}

    def fake_clone(self, source, path, **kw):
        cloned["source"] = source
        dest = root / path
        (dest / ".datalad").mkdir(parents=True)
        (dest / "sourcedata" / "ds000001" / ".datalad").mkdir(parents=True)
        (dest / "sourcedata" / "sourcedata+subjects.tsv").write_text(SUBJECTS_TSV)

    monkeypatch.setattr(add_dataset.Dataset, "clone", fake_clone, raising=False)

    add_dataset.add(
        "sourcedata/ds000001",
        "https://github.com/OpenNeuroStudies/study-ds000002",
    )

    assert cloned["source"] == "https://github.com/OpenNeuroStudies/study-ds000002"
    assert campaign_mod.read_state(root / "study-ds000002", "nprep")


def test_a_url_for_a_member_already_there_is_refused(superstudy, saves):
    with pytest.raises(SystemExit) as excinfo:
        add_dataset.add(
            "sourcedata/ds000001",
            "https://github.com/OpenNeuroStudies/study-ds000001",
        )
    assert "already exists" in str(excinfo.value)


def test_the_super_declares_the_member_as_one_of_its_outputs(superstudy, saves):
    """A newly cloned member is a new subdataset at the super, and an already-present
    one still moves its gitlink by committing the footprint. Either way the super has
    to record it, so every level ends clean rather than leaving it for publish time."""
    root, member = superstudy

    add_dataset.add("sourcedata/ds000001", "study-ds000001")

    super_call = next(call for call in saves if call[0] == root)
    declared = super_call[2]
    assert member in declared
    assert campaign_mod.campaign_dir(root, "nprep") in declared


def test_the_supers_clean_check_runs_before_the_member_changes(superstudy, saves):
    """The scopes nest: opened the other way round, the super would see its own
    intended change (the member's advanced gitlink) as pre-existing dirt."""
    root, member = superstudy
    order = []

    @contextmanager
    def recording_scope(scope_root, paths):
        order.append(("enter", scope_root))
        pending = add_dataset.utils.PendingSave()
        yield pending
        order.append(("exit", scope_root))

    add_dataset.utils.campaign_save_scope = recording_scope
    try:
        add_dataset.add("sourcedata/ds000001", "study-ds000001")
    finally:
        importlib.reload(add_dataset)

    assert order == [
        ("enter", root),  # the super checks clean while the member still is
        ("enter", member),
        ("exit", member),  # the member commits first
        ("exit", root),  # then the super records the gitlink it now points at
    ]


def test_a_members_footprint_from_another_superstudy_is_refused_not_adopted(
    superstudy, tmp_path
):
    """Reuse is right for OUR footprint and wrong for anybody else's.

    Cells get composed from this campaign's app bundle and written into whatever
    campaign dir is already there; `scaffold` then reads the app config back from
    the member. So adopting a stranger's footprint runs configs nobody chose for
    this campaign, and says nothing while it does it. Labels are short user-chosen
    words and a study accumulates campaigns by design, so the collision is ordinary.
    """
    root, member = superstudy[0], superstudy[1]

    # The member arrives already carrying a 'nprep' campaign owned by someone else —
    # as it would if cloned from a study published with its .mechababs/ committed.
    other = tmp_path / "someone-elses-super"
    other.mkdir()
    other_id = stamp_dataset_id(other, "99999999-8888-7777-6666-555555555555")
    campaign_mod.campaign_dir(member, "nprep").mkdir(parents=True, exist_ok=True)
    campaign_mod.config_path(member, "nprep").write_text(
        f"label: nprep\n{campaign_mod.SUPERSTUDY_KEY}: {other_id}\n"
    )

    with pytest.raises(SystemExit) as excinfo:
        add_dataset.write_member_footprint(root, member, "nprep")
    message = str(excinfo.value)
    assert "belongs to" in message
    # It cannot point at the owner (not on this filesystem), so it names the id
    # rather than misreporting the member as having a campaign of its own.
    assert other_id in message


def test_a_members_own_standalone_campaign_is_refused_too(superstudy):
    """The other door: not a rival superstudy, just a study that ran this label on
    its own. Same consequence, so the same refusal — and it names it accurately."""
    root, member = superstudy[0], superstudy[1]

    campaign_mod.campaign_dir(member, "nprep").mkdir(parents=True, exist_ok=True)
    campaign_mod.config_path(member, "nprep").write_text("label: nprep\n")

    with pytest.raises(SystemExit) as excinfo:
        add_dataset.write_member_footprint(root, member, "nprep")
    assert "a campaign of its own" in str(excinfo.value)
