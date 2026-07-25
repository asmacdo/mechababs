#!/usr/bin/env python3
"""Merge pipeline + cluster + dataset configs into a babs container-config YAML.

Usage:
    python3 merge_config.py \\
        --pipeline examples/pipelines/MRIQC-24.0.2.yaml \\
        --cluster examples/clusters/dartmouth.yaml \\
        --dataset-url https://github.com/OpenNeuroDatasets/ds000003.git

Writes merged YAML to stdout.

TODO: pyyaml round-trip mangles multiline block scalars (customized_text,
script_preamble get extra blank lines). Doesn't break babs (also uses
pyyaml to read) but looks ugly. See reference/babs_demo/babs_walkthrough.sh:128
for Dorota's heredoc+sed approach.
"""

import argparse
import sys

import yaml


def merge_babs_config(pipeline_config, cluster_config, dataset_url, input_origins=None,
                      campaign_venv=None):
    """Merge pipeline and cluster configs with dataset URL into a babs config dict."""
    # Pipeline config (bids_app_args, singularity_args, zip_foldernames, etc.)
    # Strip 'mechababs' — the mechababs-only namespace (container = babs-init CLI
    # args; selection = eligibility rule), consumed by mechababs, never babs config.
    merged = {k: v for k, v in pipeline_config.items() if k != "mechababs"}

    # Cluster config (cluster_resources, script_preamble, job_compute_space)
    for k, v in cluster_config.items():
        merged[k] = v

    # Resolve the venv placeholder in the preamble with the campaign venv abspath
    # (the mechababs config keeps it relative; the caller resolves against the campaign root).
    if campaign_venv and "script_preamble" in merged:
        merged["script_preamble"] = merged["script_preamble"].replace(
            "{{MECHABABS_VENV}}", campaign_venv)

    # Preserve any input_datasets already declared in the pipeline YAML
    # (e.g. chained-pipeline anat-input for fmriprep-full). Always add
    # or overwrite the BIDS entry from --dataset-url, and force BIDS
    # to be FIRST in the dict — babs uses input_datasets[0] as the
    # bids_dir positional arg to the BIDS app
    # (generate_bidsapp_runscript.py:244 on add-containers-run-v2).
    yaml_input_datasets = merged.get("input_datasets") or {}
    input_datasets = {
        "BIDS": {
            "is_zipped": False,
            "origin_url": dataset_url,
            "path_in_babs": "sourcedata/raw",
        }
    }
    for k, v in yaml_input_datasets.items():
        if k != "BIDS":
            input_datasets[k] = v

    # Chained-input origin_urls: a downstream YAML declares the upstream stage's
    # output as an input but leaves origin_url out — the upstream RIA doesn't
    # exist until it has run + merged, so the key (== the producing pipeline's
    # short_name) is the only thing knowable at author time. iterate resolves each
    # such upstream's output-RIA URL at scaffold time and passes it as
    # --input-origin <key>=<url>; nothing pipeline-specific is hardcoded here.
    for key, url in (input_origins or {}).items():
        if key not in input_datasets:
            sys.exit(f"merge_config: --input-origin {key!r} given but pipeline declares "
                     f"no {key!r} input_dataset")
        input_datasets[key]["origin_url"] = url

    merged["input_datasets"] = input_datasets

    return merged


def main():
    parser = argparse.ArgumentParser(description="Merge babs config from pipeline + cluster + dataset")
    parser.add_argument("--pipeline", required=True, help="Path to pipeline YAML config")
    parser.add_argument("--cluster", required=True, help="Path to cluster YAML config")
    parser.add_argument("--dataset-url", required=True, help="URL or path to input BIDS dataset")
    parser.add_argument("--input-origin", action="append", default=[], metavar="KEY=URL",
                        help="Set origin_url for the named input_datasets entry (repeatable). "
                             "Used for chained inputs, whose upstream RIA URL is only known at "
                             "run time; KEY is the input_datasets key (== the producing "
                             "pipeline's short_name).")
    parser.add_argument("--campaign-venv", default=None,
                        help="abspath of the campaign venv; substitutes {{MECHABABS_VENV}} in the preamble")
    args = parser.parse_args()

    input_origins = {}
    for item in args.input_origin:
        key, sep, url = item.partition("=")
        if not sep:
            parser.error(f"--input-origin expects KEY=URL, got {item!r}")
        input_origins[key] = url

    with open(args.pipeline) as f:
        pipeline_config = yaml.safe_load(f)
    with open(args.cluster) as f:
        cluster_config = yaml.safe_load(f)

    merged = merge_babs_config(pipeline_config, cluster_config, args.dataset_url, input_origins,
                               campaign_venv=args.campaign_venv)
    yaml.dump(merged, sys.stdout, default_flow_style=False, sort_keys=False)


if __name__ == "__main__":
    main()
