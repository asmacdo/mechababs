"""Unit tests for iterate helpers that are pure once their inputs are on disk.

`study_sourcedata_url` reads the raw input URL from a cloned study's `.gitmodules`
under both naming conventions — OpenNeuroStudies (plain git) names the section by
dataset id, a datalad-built study (e.g. the e2e fixture) names it by path. Both
resolve here with only `git` + a written `.gitmodules`; no clone, no fetch.
"""

import pytest

from mechababs import iterate

ID_NAMED = (  # OpenNeuroStudies: section named by dataset id (PROD)
    '[submodule "ds005896"]\n'
    "\tpath = sourcedata/ds005896\n"
    "\turl = https://github.com/OpenNeuroDatasets/ds005896.git\n"
)
PATH_NAMED = (  # datalad-built study: section named by path (the e2e fixture)
    '[submodule "sourcedata/ds999999"]\n'
    "\tpath = sourcedata/ds999999\n"
    "\turl = /local/phantom/ds999999\n"
)


def _study(tmp_path, gitmodules):
    (tmp_path / ".gitmodules").write_text(gitmodules)
    return tmp_path


def test_id_named_section_prod(tmp_path):
    study = _study(tmp_path, ID_NAMED)
    assert (
        iterate.study_sourcedata_url(study, "ds005896")
        == "https://github.com/OpenNeuroDatasets/ds005896.git"
    )


def test_path_named_section_fixture(tmp_path):
    study = _study(tmp_path, PATH_NAMED)
    assert iterate.study_sourcedata_url(study, "ds999999") == "/local/phantom/ds999999"


def test_missing_submodule_exits(tmp_path):
    study = _study(tmp_path, ID_NAMED)
    with pytest.raises(SystemExit):
        iterate.study_sourcedata_url(study, "ds000000")
