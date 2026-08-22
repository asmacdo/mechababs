"""The three-axis composition: app x cluster x source URL -> one babs config."""

import pytest
import yaml

from mechababs import compose

APP = {
    "mechababs": {
        "selection": {},
        "container": {"source": "../containers", "name": "bids-simbids"},
    },
    "bids_app_args": {"--anat-only": ""},
    "zip_foldernames": {"SimBIDS-0.0.3+anchor": "0-0-3"},
}

CLUSTER = {
    "script_preamble": "source {{MECHABABS_VENV}}/bin/activate\n",
    "job_compute_space": "/tmp",
    "cluster_resources": {"hard_runtime_limit": "00:30:00"},
}


def test_the_mechababs_namespace_never_reaches_babs():
    """`mechababs:` is ours — container, selection rule, orchestration edge.

    babs would not know what to do with any of it, and a stray key in a babs
    config is the kind of thing that fails deep inside a job script.
    """
    merged = compose.merge_babs_config(APP, CLUSTER, "https://example.org/ds.git")
    assert "mechababs" not in merged
    assert merged["bids_app_args"] == {"--anat-only": ""}
    assert merged["job_compute_space"] == "/tmp"


def test_the_bids_input_is_first_and_carries_the_source_url():
    """babs takes `input_datasets[0]` as the app's bids_dir positional argument.

    So the raw input has to be first no matter what else the app config declares
    beside it — and it is registered by URL, never a local path, so the recorded
    provenance re-resolves off this machine.
    """
    app = {**APP, "input_datasets": {"upstream": {"is_zipped": True}}}
    merged = compose.merge_babs_config(app, CLUSTER, "https://example.org/ds.git")
    keys = list(merged["input_datasets"])
    assert keys[0] == "BIDS", keys
    assert (
        merged["input_datasets"]["BIDS"]["origin_url"] == "https://example.org/ds.git"
    )
    assert merged["input_datasets"]["BIDS"]["path_in_babs"] == "sourcedata/raw"
    assert merged["input_datasets"]["upstream"] == {"is_zipped": True}


def test_the_venv_placeholder_resolves_to_the_campaign_venv():
    """The committed cluster config stays portable; the run gets the real path."""
    merged = compose.merge_babs_config(
        APP, CLUSTER, "url", campaign_venv="/s/.mechababs/campaigns/e2e/.venv"
    )
    assert (
        merged["script_preamble"]
        == "source /s/.mechababs/campaigns/e2e/.venv/bin/activate\n"
    )


def test_a_chained_inputs_origin_url_is_injected_at_compose_time():
    """A chained input's upstream RIA does not exist until the upstream has run,
    so the app config declares the input but leaves `origin_url` out; scaffold
    resolves it and passes it here."""
    app = {**APP, "input_datasets": {"anchor": {"is_zipped": True}}}
    merged = compose.merge_babs_config(
        app, CLUSTER, "url", input_origins={"anchor": "ria+file:///x#~data"}
    )
    assert merged["input_datasets"]["anchor"]["origin_url"] == "ria+file:///x#~data"


def test_wiring_an_input_the_app_never_declared_is_refused():
    """A `depends_on` edge whose producer has no `input_datasets` entry would
    otherwise compose a config babs silently ignores."""
    with pytest.raises(SystemExit) as e:
        compose.merge_babs_config(
            APP, CLUSTER, "url", input_origins={"ghost": "ria+file:///x#~data"}
        )
    assert "ghost" in str(e.value)


def test_write_babs_config_round_trips(tmp_path):
    out = compose.write_babs_config(tmp_path / "babs.yaml", APP, CLUSTER, "url")
    written = yaml.safe_load(out.read_text())
    assert written["input_datasets"]["BIDS"]["origin_url"] == "url"
    assert "mechababs" not in written
