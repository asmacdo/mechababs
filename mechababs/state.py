"""state.py — the campaign's desc-mechababs_datasets.tsv ledger accessor.

A wide TSV: dataset/identity columns (``dataset_id``, ``study_url``,
``processing_level``, ``n_subjects``, ``n_sessions``) then a column-group per pipeline
(``<p>_babs``/``_babs-merged``). There is no status enum — a pipeline's state is
derived from which columns are populated (``babs`` -> the babs-project path, set
once scaffolded; ``babs-merged`` -> finished). The per-pipeline
inclusion size is not stored here — it lives in the pinned inclusion.csv. The
schema is **read from the file's header**, not hardcoded — ``mechababs
configure`` chooses the pipelines per campaign (writing the header via
``initial_header``), so the accessor discovers them from the columns present.

The ledger is a re-derivable cache; mutators hold the campaign-level flock
(``utils.locked``) so there is a single writer.
"""

import csv
import subprocess
from pathlib import Path

STATE_FILENAME = "desc-mechababs_datasets.tsv"
LOCK_FILENAME = "." + STATE_FILENAME + ".lock"

# The campaign's mechababs-owned dir: config + orchestration provenance
# (inclusions, babs-init configs). Hidden (dot-dir) so it stays out of the BIDS
# tree the campaign otherwise is.
MECHABABS_DIR = ".mechababs"
CONFIG_FILENAME = "campaign.yaml"

IDENTITY_COLUMNS = [
    "dataset_id",
    "study_url",
    "processing_level",
    "n_subjects",
    "n_sessions",
]
PIPELINE_COLUMNS = ["babs", "babs-merged"]


def state_path(campaign):
    return Path(campaign) / STATE_FILENAME


def config_path(campaign):
    """The campaign config written by ``configure`` (``.mechababs/campaign.yaml``)."""
    return Path(campaign) / MECHABABS_DIR / CONFIG_FILENAME


def initial_header(short_names):
    """The header line for a fresh ledger: identity columns + a group per pipeline.

    Written once at construction (``mechababs configure``); thereafter the schema
    is read back from the file (see ``header``).
    """
    cols = list(IDENTITY_COLUMNS)
    for short in short_names:
        cols += [f"{short}_{c}" for c in PIPELINE_COLUMNS]
    return "\t".join(cols) + "\n"


def header(campaign):
    """The ledger's column names, read from its header row."""
    with open(state_path(campaign), newline="") as f:
        return next(csv.reader(f, delimiter="\t"))


def pipelines(campaign):
    """Pipeline names parsed from the ``<pipeline>_babs`` header columns.

    ``_babs`` (the path column) is the per-pipeline anchor; ``_babs-merged`` does
    not end in ``_babs``, so the suffix match picks out exactly one column each.
    """
    suffix = "_babs"
    return [c[: -len(suffix)] for c in header(campaign) if c.endswith(suffix)]


def read_rows(campaign):
    with open(state_path(campaign), newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def write_rows(campaign, cols, rows):
    """Rewrite the ledger in place with the given header and rows."""
    with open(state_path(campaign), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter="\t", lineterminator="\n")
        w.writeheader()
        for row in rows:
            w.writerow({c: row.get(c, "") for c in cols})


def save(campaign, message):
    """Record the ledger change in the campaign's datalad history."""
    subprocess.run(
        [
            "datalad",
            "save",
            "--dataset",
            str(campaign),
            "--message",
            message,
            str(state_path(campaign)),
        ],
        check=True,
    )
