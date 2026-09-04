"""The scaffold verb: naming, the pin, the gate, and the one transition it makes.

The `babs init` shell-out is stubbed. What is being tested is mechababs' half —
which subjects, which config, which directory, which column — and a real babs init
belongs to the e2e, where there is a real study and a real container to init from.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import stamp_dataset_id
from mechababs import campaign as campaign_mod
from mechababs import scaffold

LABEL = "e2e"
ANCHOR = "bids-app-configs/SimBIDS-0.0.3+anchor.yaml"
CHAIN = "bids-app-configs/SimBIDS-0.0.3+chain.yaml"
SOURCEDATA = "sourcedata/ds999999"

ANCHOR_CONFIG = {
    "mechababs": {
        "selection": {},
        "container": {"source": "../containers", "name": "bids-simbids"},
    },
    "bids_app_args": {"--anat-only": ""},
    "output_ria_path": ".babs/output_ria",
}

CHAIN_CONFIG = {
    **ANCHOR_CONFIG,
    "mechababs": {**ANCHOR_CONFIG["mechababs"], "depends_on": "SimBIDS-0.0.3+anchor"},
    "input_datasets": {"SimBIDS-0.0.3+anchor": {"is_zipped": True}},
}

CLUSTER_CONFIG = {
    "script_preamble": "activate {{MECHABABS_VENV}}\n",
    "job_compute_space": "/tmp",
}

SUBJECTS_TSV = (
    "subject_id\tdatatypes\tt1w_num\tbold_num\n"
    "sub-01\tanat,func\t1\t1\n"
    "sub-02\tanat,func\t1\t1\n"
)


# --------------------------------------------------------------------------
# Naming, pins, URLs — the derivations, tested without a study on disk
# --------------------------------------------------------------------------


def test_a_named_sourcedata_carries_its_id_into_the_derivative():
    """A cell is (source dataset x app), so the app stem alone would collide the
    moment a study holds two source datasets."""
    assert (
        scaffold.derivative_name("sourcedata/ds000001", ANCHOR, LABEL)
        == "SimBIDS-0.0.3+anchor+ds000001+e2e"
    )
    assert (
        scaffold.derivative_path("sourcedata/ds000001", ANCHOR, LABEL)
        == "derivatives/SimBIDS-0.0.3+anchor+ds000001+e2e"
    )


def test_the_campaign_label_comes_last():
    """A study accumulates campaigns; without the label a second campaign could
    not produce the same cell until the first one's derivative was retired."""
    assert (
        scaffold.derivative_name("sourcedata/ds000001", ANCHOR, "c2")
        == "SimBIDS-0.0.3+anchor+ds000001+c2"
    )


@pytest.mark.parametrize("slot", ["sourcedata/raw", "sourcedata/rawbids"])
def test_a_generic_sourcedata_slot_carries_no_id(slot):
    """`raw`/`rawbids` are slots, not dataset ids — there is nothing to collide
    with, and nothing meaningful to put in the name."""
    assert scaffold.derivative_name(slot, ANCHOR, LABEL) == "SimBIDS-0.0.3+anchor+e2e"


def test_the_inclusion_pin_is_filename_safe_and_keeps_the_whole_path(tmp_path):
    """Both halves of a cell's identity are paths; the pin has to be one filename.

    The whole sourcedata path is kept (with `/` -> `-`) so two datasets whose
    directories share a basename cannot land on the same pin.
    """
    pin = scaffold.inclusion_pin(tmp_path, LABEL, "sourcedata/sub/ds1", ANCHOR)
    other = scaffold.inclusion_pin(tmp_path, LABEL, "sourcedata/ds1", ANCHOR)
    assert pin.name == "sourcedata-sub-ds1_SimBIDS-0.0.3+anchor.csv"
    assert pin != other
    assert pin.parent == campaign_mod.inclusions_dir(tmp_path, LABEL)


def test_a_container_url_is_passed_through_but_a_relative_path_resolves(tmp_path):
    """A production config names a URL; a dev one names a sibling checkout, which
    is resolved against the STUDY root so nothing absolute is committed."""
    assert (
        scaffold.resolve_container_ds(
            tmp_path, {"source": "https://github.com/ReproNim/containers.git"}
        )
        == "https://github.com/ReproNim/containers.git"
    )
    assert scaffold.resolve_container_ds(tmp_path, {"source": "../containers"}) == str(
        (tmp_path / "../containers").resolve()
    )
    assert (
        scaffold.resolve_container_ds(tmp_path, {"source": "/abs/containers"})
        == "/abs/containers"
    )


def test_the_source_url_comes_from_the_studys_own_gitmodules(tmp_path):
    """Registered by URL, not local path — so what babs records re-resolves off
    this machine. Both `.gitmodules` naming conventions are accepted."""
    (tmp_path / ".gitmodules").write_text(
        '[submodule "sourcedata/ds999999"]\n'
        "\tpath = sourcedata/ds999999\n"
        "\turl = https://github.com/OpenNeuroDatasets/ds999999.git\n"
    )
    assert (
        scaffold.source_dataset_url(tmp_path, SOURCEDATA)
        == "https://github.com/OpenNeuroDatasets/ds999999.git"
    )

    (tmp_path / ".gitmodules").write_text(  # OpenNeuroStudies names by id
        '[submodule "ds999999"]\n'
        "\turl = https://github.com/OpenNeuroDatasets/ds999999.git\n"
    )
    assert (
        scaffold.source_dataset_url(tmp_path, SOURCEDATA)
        == "https://github.com/OpenNeuroDatasets/ds999999.git"
    )


def test_a_sourcedata_that_is_not_a_registered_subdataset_is_refused(tmp_path):
    (tmp_path / ".gitmodules").write_text("")
    with pytest.raises(SystemExit, match="no submodule url"):
        scaffold.source_dataset_url(tmp_path, SOURCEDATA)


# --------------------------------------------------------------------------
# A study + campaign on disk, enough for the transition
# --------------------------------------------------------------------------


@pytest.fixture
def study(tmp_path):
    """A study with a two-app campaign and both cells added, `babs` empty."""
    study = tmp_path / "study-ds999999"
    campaign = campaign_mod.campaign_dir(study, LABEL)
    (campaign / campaign_mod.APPS_DIRNAME).mkdir(parents=True)
    (campaign / campaign_mod.CLUSTERS_DIRNAME).mkdir(parents=True)

    (campaign / ANCHOR).write_text(yaml.safe_dump(ANCHOR_CONFIG))
    (campaign / CHAIN).write_text(yaml.safe_dump(CHAIN_CONFIG))
    (campaign / "clusters/test.yaml").write_text(yaml.safe_dump(CLUSTER_CONFIG))
    campaign_mod.config_path(study, LABEL).write_text(
        yaml.safe_dump(
            {
                "label": LABEL,
                "apps": [ANCHOR, CHAIN],
                "cluster": "clusters/test.yaml",
                "limit": 1,
            }
        )
    )

    (study / "sourcedata").mkdir(parents=True)
    (study / "sourcedata/sourcedata+subjects.tsv").write_text(SUBJECTS_TSV)
    (study / ".gitmodules").write_text(
        f'[submodule "{SOURCEDATA}"]\n\turl = https://example.org/ds999999.git\n'
    )

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


def _stub_babs(monkeypatch, on_call):
    """Intercept the `babs` shell-out only; everything else really runs.

    `scaffold` also shells out to `git config` to read the source URL out of
    `.gitmodules`, so a blanket subprocess stub would break the thing under test.
    """
    real_run = subprocess.run

    def dispatch(cmd, **kwargs):
        # Matched by basename: scaffold resolves babs beside sys.prefix, so the
        # argv carries an absolute path (and the test venv has no babs to run).
        if Path(str(cmd[0])).name != "babs":
            return real_run(cmd, **kwargs)
        on_call(cmd, kwargs)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(scaffold.subprocess, "run", dispatch)


@pytest.fixture
def babs_calls(monkeypatch):
    """Stub `babs init`; collect the argv and the config file it was handed.

    The composed config is a tempfile that is gone by the time the test looks, so
    its content is read here, while the call is in flight.
    """
    calls = []

    def record(cmd, kwargs):
        with open(cmd[cmd.index("--container-config") + 1]) as f:
            config = yaml.safe_load(f)
        calls.append({"cmd": cmd, "kwargs": kwargs, "config": config})

    _stub_babs(monkeypatch, record)
    return calls


def _row(study, app_config):
    return next(
        r
        for r in campaign_mod.read_state(study, LABEL)
        if r["app_config"] == app_config
    )


def test_scaffold_inits_the_derivative_and_records_the_cell(study, babs_calls):
    project = scaffold.scaffold(study, LABEL, SOURCEDATA, ANCHOR)

    assert project == "derivatives/SimBIDS-0.0.3+anchor+ds999999+e2e"
    assert _row(study, ANCHOR)["babs"] == project, "the cell was not recorded"
    assert _row(study, CHAIN)["babs"] == "", "a sibling cell was touched"

    (call,) = babs_calls
    cmd = call["cmd"]
    assert Path(cmd[0]).name == "babs" and cmd[1:3] == ["init", project], cmd
    assert call["kwargs"]["cwd"] == str(study)
    # Study-relative, because the recorded command has to re-execute elsewhere.
    assert (
        cmd[cmd.index("--list-sub-file") + 1]
        == ".mechababs/campaigns/e2e/inclusions/sourcedata-ds999999_SimBIDS-0.0.3+anchor.csv"
    )
    assert cmd[cmd.index("--processing-level") + 1] == "subject"
    assert cmd[cmd.index("--container-name") + 1] == "bids-simbids"


def test_the_generated_inclusion_honours_the_campaigns_limit(study, babs_calls):
    scaffold.scaffold(study, LABEL, SOURCEDATA, ANCHOR)
    pin = scaffold.inclusion_pin(study, LABEL, SOURCEDATA, ANCHOR)
    assert pin.read_text().split() == ["sub_id", "sub-01"], (
        "limit: 1 did not cap the list to the first eligible subject"
    )


def test_a_pinned_inclusion_is_used_as_is(study, babs_calls):
    """The smoke-test affordance: hand-write one row before the first iterate and the
    whole pipeline runs on that subject. Selection is skipped entirely."""
    pin = scaffold.inclusion_pin(study, LABEL, SOURCEDATA, ANCHOR)
    pin.parent.mkdir(parents=True)
    pin.write_text("sub_id\nsub-02\n")
    scaffold.scaffold(study, LABEL, SOURCEDATA, ANCHOR)
    assert pin.read_text() == "sub_id\nsub-02\n", "the pin was regenerated"


def test_the_composed_config_reaches_babs_and_carries_all_three_axes(study, babs_calls):
    scaffold.scaffold(study, LABEL, SOURCEDATA, ANCHOR)

    config = babs_calls[0]["config"]
    assert config["bids_app_args"] == {"--anat-only": ""}  # app axis
    assert config["job_compute_space"] == "/tmp"  # cluster axis
    assert (
        config["input_datasets"]["BIDS"]["origin_url"]
        == "https://example.org/ds999999.git"
    )  # source axis
    assert str(campaign_mod.venv_path(study, LABEL)) in config["script_preamble"]
    assert "mechababs" not in config


def test_a_members_job_scripts_name_the_superstudys_venv(study, babs_calls):
    """The venv path is baked into every job script, so it has to name the level the
    campaign is operated from.

    A member of a super-campaign is given no environment of its own, so a
    member-level path leaves every job activating a file that does not exist and
    dying at its first command. The one cluster config the e2e runs
    (`examples/clusters/test-docker.yaml`) hardcodes its PATH instead of using the
    placeholder, so the substitution is exercised nowhere but here.
    """
    config = campaign_mod.config_path(study, LABEL)
    config.write_text(
        config.read_text()
        + f"{campaign_mod.SUPERSTUDY_KEY}: {stamp_dataset_id(study.parent)}\n"
    )

    scaffold.scaffold(study, LABEL, SOURCEDATA, ANCHOR)

    preamble = babs_calls[0]["config"]["script_preamble"]
    assert str(campaign_mod.venv_path(study.parent, LABEL)) in preamble
    assert str(campaign_mod.venv_path(study, LABEL)) not in preamble


def test_an_already_scaffolded_cell_is_refused(study, babs_calls):
    """The self-guard. A stray `datalad rerun` onto current HEAD lands here, and
    has to fail loudly rather than init a second derivative over the first."""
    scaffold.scaffold(study, LABEL, SOURCEDATA, ANCHOR)
    with pytest.raises(SystemExit, match="already scaffolded"):
        scaffold.scaffold(study, LABEL, SOURCEDATA, ANCHOR)
    assert len(babs_calls) == 1, "the refused re-scaffold still ran babs init"


def test_a_cell_that_is_not_in_the_shard_is_refused(study, babs_calls):
    with pytest.raises(SystemExit, match="no cell for"):
        scaffold.scaffold(study, LABEL, "sourcedata/nope", ANCHOR)
    assert babs_calls == []


def test_a_dependent_cell_is_refused_while_its_producer_is_unmerged(study, babs_calls):
    """The reconciler notes "waiting on producer" and moves on; this verb, reached
    only because something decided the cell was ready, must be loud instead."""
    with pytest.raises(SystemExit, match="not merged yet"):
        scaffold.scaffold(study, LABEL, SOURCEDATA, CHAIN)
    assert babs_calls == []
    assert _row(study, CHAIN)["babs"] == ""


def _merge_the_producer(study):
    rows = campaign_mod.read_state(study, LABEL)
    rows[0]["babs"] = "derivatives/SimBIDS-0.0.3+anchor+ds999999+e2e"
    rows[0]["merged"] = "true"
    campaign_mod.write_state(study, LABEL, rows)


def test_a_merged_producer_wires_its_output_ria_into_the_dependent(study, babs_calls):
    """`input_datasets` names the producer, so its merged output store is wired in."""
    _merge_the_producer(study)
    scaffold.scaffold(study, LABEL, SOURCEDATA, CHAIN)

    origin = babs_calls[0]["config"]["input_datasets"]["SimBIDS-0.0.3+anchor"][
        "origin_url"
    ]
    assert origin.startswith("ria+file://"), origin
    assert origin.endswith("/.babs/output_ria#~data"), origin
    assert "derivatives/SimBIDS-0.0.3+anchor+ds999999+e2e" in origin


def test_a_depends_on_edge_alone_wires_nothing(study, babs_calls):
    """`depends_on` is ordering ONLY — it carries no kind and wires nothing.

    A QC gate (mriqc gating fmriprep) is exactly this shape: the producer must be
    merged first, and its output is never an input. Wiring comes from
    `input_datasets` and from nowhere else, so dropping that declaration leaves
    the edge ordering the two cells and touching no input.
    """
    gate_config = {k: v for k, v in CHAIN_CONFIG.items() if k != "input_datasets"}
    (campaign_mod.campaign_dir(study, LABEL) / CHAIN).write_text(
        yaml.safe_dump(gate_config)
    )
    _merge_the_producer(study)
    scaffold.scaffold(study, LABEL, SOURCEDATA, CHAIN)

    assert list(babs_calls[0]["config"]["input_datasets"]) == ["BIDS"], (
        "a depends_on edge wired the producer as an input"
    )


def test_an_input_naming_no_cell_is_left_to_carry_its_own_origin(study, babs_calls):
    """An `input_datasets` key that matches no cell is an input from OUTSIDE the
    campaign — a precomputed derivative, say. It keeps the origin_url the config
    gives it, and no cell is looked for."""
    external = {
        **ANCHOR_CONFIG,
        "input_datasets": {
            "priors": {"is_zipped": False, "origin_url": "https://example.org/priors"}
        },
    }
    (campaign_mod.campaign_dir(study, LABEL) / ANCHOR).write_text(
        yaml.safe_dump(external)
    )
    scaffold.scaffold(study, LABEL, SOURCEDATA, ANCHOR)

    inputs = babs_calls[0]["config"]["input_datasets"]
    assert inputs["priors"]["origin_url"] == "https://example.org/priors"


def test_an_input_whose_producer_cell_is_unmerged_is_refused(study, babs_calls):
    """The wiring's own check, reached without consulting `depends_on`.

    A config that declares the input and forgets the ordering edge would otherwise
    hand babs an input with no origin — quieter, and worse, than refusing.
    """
    unordered = {k: v for k, v in CHAIN_CONFIG.items()}
    unordered["mechababs"] = {
        k: v for k, v in CHAIN_CONFIG["mechababs"].items() if k != "depends_on"
    }
    (campaign_mod.campaign_dir(study, LABEL) / CHAIN).write_text(
        yaml.safe_dump(unordered)
    )
    rows = campaign_mod.read_state(study, LABEL)
    rows[1]["depends_on"] = ""  # the statefile mirrors the config's declaration
    rows[0]["babs"] = "derivatives/SimBIDS-0.0.3+anchor+ds999999+e2e"
    campaign_mod.write_state(study, LABEL, rows)

    with pytest.raises(SystemExit, match="not merged yet"):
        scaffold.scaffold(study, LABEL, SOURCEDATA, CHAIN)
    assert babs_calls == []
