"""compose.py — the babs container-config, composed from the campaign's axes.

`babs init --container-config` takes ONE YAML. A campaign holds it as three
separable things, and they are separable because they change for different
reasons:

- the **app config** (`bids-app-configs/<name>.yaml`) — the BIDS app's flags, its
  container, its zip foldernames. Travels with the campaign, reviewed as science.
- the **cluster config** (`clusters/<name>.yaml`) — SLURM resources, the job-script
  preamble, where per-job scratch lives. A site fact, often private.
- the **source dataset** — registered by *URL*, so the recorded provenance
  re-resolves somewhere other than the machine that ran it.

Never bake a cluster detail into an app config or vice versa: the whole point of
the split is that the same app runs on another site by swapping one file.

Composition is a pure function of those three (plus the campaign venv path and,
for a chained cell, the upstream's resolved output-RIA URL), so the result is
*derived* — scaffold composes it into a tempfile per run rather than committing a
copy. babs keeps its own resolved copy at the derivative's
`.babs/babs_init_config.yaml`, which is where the exact config that ran belongs.
"""

import sys

import yaml

# The app config's mechababs-only namespace: `container` (which image to hand
# `babs init`), `selection` (the eligibility rule scaffold applies), `depends_on`
# (the orchestration edge). None of it is babs config, so it is stripped here —
# the one place that knows the boundary.
MECHABABS_NAMESPACE = "mechababs"

# babs uses `input_datasets[0]` as the BIDS-app's `bids_dir` positional argument,
# so the raw input must be FIRST in the mapping regardless of what the app config
# declares alongside it.
BIDS_INPUT_KEY = "BIDS"
BIDS_PATH_IN_BABS = "sourcedata/raw"

# Substituted in the cluster's `script_preamble` with the campaign venv's path.
# The config stays portable (no abspath committed); the run gets the real thing.
VENV_PLACEHOLDER = "{{MECHABABS_VENV}}"


def merge_babs_config(
    app_config, cluster_config, source_url, *, input_origins=None, campaign_venv=None
):
    """The babs config for one cell: app x cluster x source URL, as a dict.

    ``input_origins`` maps an ``input_datasets`` key to the URL scaffold resolved
    for it at run time — a chained cell's upstream output RIA, which cannot be
    written in the YAML because it does not exist until the upstream has run.
    """
    merged = {k: v for k, v in app_config.items() if k != MECHABABS_NAMESPACE}
    merged.update(cluster_config)

    if campaign_venv and "script_preamble" in merged:
        merged["script_preamble"] = merged["script_preamble"].replace(
            VENV_PLACEHOLDER, str(campaign_venv)
        )

    declared = merged.get("input_datasets") or {}
    input_datasets = {
        BIDS_INPUT_KEY: {
            "is_zipped": False,
            "origin_url": source_url,
            "path_in_babs": BIDS_PATH_IN_BABS,
        }
    }
    for key, value in declared.items():
        if key != BIDS_INPUT_KEY:
            input_datasets[key] = value

    for key, url in (input_origins or {}).items():
        if key not in input_datasets:
            sys.exit(
                f"cannot wire input {key!r}: the app config declares no "
                f"input_datasets entry by that name (it declares: "
                f"{', '.join(sorted(declared)) or 'none'})"
            )
        input_datasets[key]["origin_url"] = url

    merged["input_datasets"] = input_datasets
    return merged


def write_babs_config(
    path,
    app_config,
    cluster_config,
    source_url,
    *,
    input_origins=None,
    campaign_venv=None,
):
    """Compose and write the babs config to ``path``. Returns ``path``."""
    merged = merge_babs_config(
        app_config,
        cluster_config,
        source_url,
        input_origins=input_origins,
        campaign_venv=campaign_venv,
    )
    with open(path, "w") as f:
        yaml.safe_dump(merged, f, default_flow_style=False, sort_keys=False)
    return path
