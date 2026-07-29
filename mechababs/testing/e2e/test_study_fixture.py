"""Shape check for the `study` fixture — the fake OpenNeuroStudies-shaped study the
campaign clones and inits its derivative into.

Pure test-infra coverage: asserts the fixture builds a faithful study (upstream
dataset_description shape, the raw registered as a nested subdataset, the metadata
TSV `select` reads). No campaign / production code is exercised here.
"""

import csv
import json


def test_study_fixture_shape(study):
    # Study-level description: the upstream OpenNeuroStudies shape (a study, made by
    # openneuro-studies), which mechababs clones and never authors in prod.
    desc = json.loads((study / "dataset_description.json").read_text())
    assert desc["DatasetType"] == "study"
    assert desc["GeneratedBy"][0]["Name"] == "openneuro-studies"

    # The raw phantom is registered as a nested subdataset, not a plain dir — the
    # .gitmodules entry is the proof the campaign will clone a real 3-deep nest.
    gitmodules = (study / ".gitmodules").read_text()
    assert "sourcedata/ds999999" in gitmodules

    # Metadata must be git-tracked, not annexed — an annex symlink would clone in
    # broken (no content), so add-dataset's study clone would have no readable
    # description or TSV. A regular file (not a symlink) proves it's in git.
    desc_path = study / "dataset_description.json"
    tsv = study / "sourcedata" / "sourcedata+subjects.tsv"
    assert not desc_path.is_symlink(), "dataset_description.json is annexed, not in git"
    assert not tsv.is_symlink(), "sourcedata+subjects.tsv is annexed, not in git"

    # The metadata TSV `select` reads, with the columns its eligibility filters key
    # on; at least one subject row, and the phantom is anatomical (t1w present).
    rows = list(csv.DictReader(tsv.open(), delimiter="\t"))
    assert rows, "no subject rows in the study metadata TSV"
    assert {"subject_id", "datatypes", "t1w_num", "bold_num"} <= set(rows[0])
    assert any(int(r["t1w_num"]) > 0 for r in rows), "phantom has no T1w"
