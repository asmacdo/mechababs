"""Unit tests for construct's config resolution.

Configs resolve by name under the campaign's own clusters/ and pipelines/ (not the
vendored tool), so a run reproduces from the campaign alone; ``configure`` copies a
config given by path into the campaign.
"""

import pytest

from mechababs import construct


def test_stage_config_copies_a_path_into_the_campaign(tmp_path):
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    src = tmp_path / "elsewhere" / "sherlock.yaml"
    src.parent.mkdir()
    src.write_text("x: 1\n")
    assert construct.stage_config(campaign, "clusters", str(src)) == (
        "clusters/sherlock.yaml", True
    )
    assert (campaign / "clusters" / "sherlock.yaml").read_text() == "x: 1\n"


def test_stage_config_resolves_a_name_already_in_the_campaign(tmp_path):
    (tmp_path / "clusters").mkdir()
    (tmp_path / "clusters" / "sherlock.yaml").write_text("x: 1\n")
    # a bare name is resolved in place, nothing copied
    assert construct.stage_config(tmp_path, "clusters", "sherlock.yaml") == (
        "clusters/sherlock.yaml", False
    )


def test_stage_config_does_not_recopy_a_config_already_in_place(tmp_path):
    (tmp_path / "clusters").mkdir()
    present = tmp_path / "clusters" / "sherlock.yaml"
    present.write_text("x: 1\n")
    # the path of a file already under clusters/ resolves in place, not a self-copy
    assert construct.stage_config(tmp_path, "clusters", str(present)) == (
        "clusters/sherlock.yaml", False
    )


def test_stage_config_missing_config_exits(tmp_path):
    with pytest.raises(SystemExit):
        construct.stage_config(tmp_path, "clusters", "not-here.yaml")


def test_resolve_pipelines_returns_campaign_paths(tmp_path):
    (tmp_path / "pipelines").mkdir()
    (tmp_path / "pipelines" / "MRIQC-24.0.2.yaml").write_text("x: 1\n")
    # campaign-relative, not code/mechababs/pipelines/...; nothing copied
    assert construct.resolve_pipelines(tmp_path, ["MRIQC-24.0.2.yaml"]) == (
        ["pipelines/MRIQC-24.0.2.yaml"], []
    )


def test_resolve_pipelines_rejects_duplicate_stem_before_staging(tmp_path):
    # two different files sharing a basename: rejected on the name, and — because
    # validation precedes staging — neither is copied into the campaign.
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    a = tmp_path / "a" / "P.yaml"
    a.parent.mkdir()
    a.write_text("x: 1\n")
    b = tmp_path / "b" / "P.yaml"
    b.parent.mkdir()
    b.write_text("x: 2\n")
    with pytest.raises(SystemExit):
        construct.resolve_pipelines(campaign, [str(a), str(b)])
    assert not (campaign / "pipelines" / "P.yaml").exists()
