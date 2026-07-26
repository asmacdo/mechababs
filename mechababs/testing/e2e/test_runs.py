"""Full-run scenario: drive the campaign CLI through the whole happy path and assert.

configure -> add-dataset -> iterate (scaffold via `babs init`) -> iterate (submit)
-> `babs status --wait` (block until the jobs finish) -> iterate (merge). Asserts the
ledger + babs project at scaffold, then the merged column + a produced derivative.

This is the piece that exercises the reconciler's ACTIVE-cell path
(decide_action -> babs_status.read_status -> decide -> ITERATE_ACTIONS): the second
tick decides "submit", the third decides "merge". Full-run tier = simbids only (light
enough to actually submit+run under the slurm-docker-ci slurm); the scaffold
assertions here double as the scaffold tier.
"""

import csv
import json
import logging
import os
import subprocess

import yaml

from mechababs import state

log = logging.getLogger("mechababs.e2e")

# simbids pipeline short_names (ledger column prefix + derivative dir name). STAGE1
# is the base pipeline (test_full_run) and the chained-inputs producer; STAGE2
# (test_chained_run) consumes STAGE1's output by name.
STAGE1 = "SimBIDS-0.0.3+anchor"
STAGE2 = "SimBIDS-0.0.3+chain"


def _venv_run(campaign, tool, *args):
    """Run a campaign-venv tool (`mechababs` or `babs`) with the venv on PATH.

    The venv-bin binary satisfies configure's sys.prefix guard; prepending the venv
    bin makes `babs`/`duct` resolve there too (iterate's assert_venv_tools), mirroring
    an activated venv. Output is left uncaptured so it streams under `pytest -s` (and
    shows on failure otherwise).
    """
    venv_bin = campaign / ".venv" / "bin"
    # Bypass iterate's interactive tmux guard: the harness drives iterate through an
    # inherited stdin, where a stray keystroke buffered during a long tick would be
    # read as the guard's answer and abort the run.
    env = {
        **os.environ,
        "PATH": f"{venv_bin}:{os.environ['PATH']}",
        "MECHABABS_SKIP_TMUX_CHECK": "1",
    }
    log.info("%s %s", tool, " ".join(str(a) for a in args))
    return subprocess.run(
        [str(venv_bin / tool), *args],
        cwd=campaign,
        env=env,
        check=True,
        text=True,
    )


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _assert_nest_clean(levels, phase):
    """Every level of the nest is clean after a record-up. Campaign-porcelain bubbles
    sub-dirt up as a modified submodule, but check each level so a future submodule
    `ignore=` can't mask it — and so a failure names the level that's dirty."""
    for level in levels:
        assert not _git(level, "status", "--porcelain").strip(), (
            f"{level.name} dirty after {phase} — record-up left a level uncommitted"
        )


def _first_subject(rawdata):
    subs = sorted(p.name for p in rawdata.iterdir() if p.name.startswith("sub-"))
    assert subs, f"no sub-* under {rawdata} — host fixtures not mounted?"
    return subs[0]


def _ledger_row(campaign):
    """The single ledger row (the fixture registers exactly one dataset)."""
    rows = list(
        csv.DictReader((campaign / state.STATE_FILENAME).open(), delimiter="\t")
    )
    assert len(rows) == 1, f"expected one ledger row, got {len(rows)}"
    return rows[0]


def test_full_run(campaign, cluster_config, rawdata, study):
    _venv_run(
        campaign,
        "mechababs",
        "configure",
        "--pipelines",
        f"code/mechababs/examples/pipelines/{STAGE1}.yaml",
        "--cluster",
        f"code/mechababs/examples/clusters/{cluster_config}",
        "--limit",
        "1",
    )
    # configure made the campaign a BIDS study: dataset_description names mechababs.
    camp_desc = json.loads((campaign / "dataset_description.json").read_text())
    assert camp_desc["DatasetType"] == "study", "campaign not a BIDS study"
    assert camp_desc["GeneratedBy"][0]["Name"] == "mechababs", (
        "campaign dataset_description missing the mechababs GeneratedBy agent"
    )

    _venv_run(
        campaign,
        "mechababs",
        "add-dataset",
        str(rawdata),
        "--study",
        str(study),
        "--processing-level",
        "subject",
    )

    # add-dataset cloned the study into the campaign and recorded its URL.
    study_ds = campaign / "studies" / "study-ds999999"
    assert (study_ds / "dataset_description.json").is_file(), (
        "add-dataset did not clone the study into studies/"
    )
    assert "studies/study-ds999999" in (campaign / ".gitmodules").read_text(), (
        "study not registered as a campaign subdataset"
    )
    assert _ledger_row(campaign)["study_url"] == str(study), (
        "add-dataset did not record study_url"
    )

    # simbids has `selection: {}` (pass-through), so `--limit 1` selects the first
    # subject — which _first_subject also returns — and iterate generates the
    # inclusion itself.
    sub = _first_subject(rawdata)

    # --- tick 1: scaffold (not-started -> `babs init`, no submit) ---
    _venv_run(campaign, "mechababs", "iterate", "--batch", "1")

    row = _ledger_row(campaign)
    assert row["processing_level"] == "subject"
    proj_rel = row[f"{STAGE1}_babs"]
    assert proj_rel, f"iterate did not record {STAGE1}_babs — scaffold failed"
    # The derivative is produced in its final home inside the cloned study, named
    # by the pipeline's short_name.
    assert proj_rel == f"studies/study-ds999999/derivatives/{STAGE1}", (
        f"scaffold did not init into the study's derivatives/: {proj_rel}"
    )

    proj = campaign / proj_rel
    # BIDS-study layout (the simbids pipeline sets analysis_path: "." + .babs/ RIA
    # stores): the project root IS the analysis dataset, so code/ is at its root
    # and the RIA stores tuck under .babs/.
    assert (proj / ".babs" / "input_ria").is_dir()
    assert (proj / ".babs" / "output_ria").is_dir()
    code = proj / "code"
    assert (code / "babs_proj_config.yaml").is_file()
    # babs records the subjects it will process in the derivative's inclusion.
    assert sub in (code / "processing_inclusion.csv").read_text()
    assert not row.get(f"{STAGE1}_babs-merged"), "merged before any job ran"

    # --- record-up: the scaffold advance is committed UP the nest, not just on disk ---
    # The datalad_save_scope's recursive save must register the fresh derivative into
    # the study and bump the gitlink campaign <- study <- derivative in one node,
    # leaving every level clean (green == tracked).
    _assert_nest_clean([proj, study_ds, campaign], "scaffold")
    assert f"derivatives/{STAGE1}" in (study_ds / ".gitmodules").read_text(), (
        "study did not register the derivative as a subdataset"
    )
    head_msg = _git(campaign, "log", "--first-parent", "-1", "--format=%s").strip()
    assert head_msg == f"scaffold ds999999/{STAGE1}", (
        f"campaign mainline did not record the scaffold node: {head_msg!r}"
    )
    # make sure the RIA stores are never committed (babs tracks .babs/babs_init_config.yaml
    # itself, so scope to the input_ria/output_ria symlink stores, not all of .babs).
    assert not _git(
        proj, "ls-files", "--", ".babs/input_ria", ".babs/output_ria"
    ).strip(), "RIA stores committed to git"

    # --- tick 2: active cell, nothing submitted -> decide "submit" -> `babs submit` ---
    _venv_run(campaign, "mechababs", "iterate", "--batch", "1")

    # --- block until the submitted job reaches a terminal state (done or failed) ---
    _venv_run(campaign, "babs", "status", "--wait", "--wait-interval", "5", str(proj))

    # --- tick 3: active cell, all done -> decide "merge" -> `babs merge` ---
    _venv_run(campaign, "mechababs", "iterate", "--batch", "1")

    # --- the ledger records the merge, and a derivative was produced ---
    row = _ledger_row(campaign)
    assert row[f"{STAGE1}_babs-merged"] == "true", (
        f"merge tick did not set {STAGE1}_babs-merged (row={row})"
    )

    # merge recorded UP the nest, same as scaffold: `datalad update --how merge`
    # COMMITTED the derivative advance, so the recursive save propagates that committed
    # advance study -> campaign in one node — every level clean, merge the latest node.
    _assert_nest_clean([proj, study_ds, campaign], "merge")
    assert (
        _git(campaign, "log", "--first-parent", "-1", "--format=%s").strip()
        == f"merge ds999999/{STAGE1}"
    ), "campaign mainline did not record the merge node"

    # `babs merge` deposits the merged results in the OUTPUT RIA (not the analysis
    # working tree). The RIA store holds one bare dataset repo at
    # .babs/output_ria/<uuid[:3]>/<uuid[3:]>; its default-branch tree lists the zip.
    # Checking it there proves the derivative was produced and merged, not just that
    # the ledger flag flipped.
    # (the RIA also keeps an `alias/data` symlink to the dataset — skip it.)
    ria_repos = [
        p.parent
        for p in (proj / ".babs" / "output_ria").glob("*/*/HEAD")
        if "alias" not in p.parts
    ]
    assert len(ria_repos) == 1, (
        f"expected one output-RIA dataset repo, found {ria_repos}"
    )
    tree = subprocess.run(
        # HEAD, not "master": the RIA repo's default branch follows git's
        # init.defaultBranch (modern git -> "main"), so name it branch-agnostically.
        ["git", f"--git-dir={ria_repos[0]}", "ls-tree", "-r", "--name-only", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"{sub}_" in tree and ".zip" in tree, (
        f"merge produced no {sub} derivative zip in the output RIA:\n{tree}"
    )

    # --- retire the derivative: out of the study, into derivative-attempts/ ---
    # A cell that must be redone (a resource change, a tool bug) has to leave the
    # study without losing its evidence — logs, history, run records. This asserts
    # the move AND the ledger reset land in one transition, so the cell is never
    # half-retired (gone from disk but still routed as in-progress). It runs last
    # because it deliberately leaves this campaign derivative-less.
    deriv_id = _git(
        study_ds, "config", "-f", ".gitmodules",
        "--get", f"submodule.derivatives/{STAGE1}.datalad-id",
    ).strip()
    _venv_run(
        campaign, "mechababs", "retire-derivative",
        f"studies/study-ds999999/derivatives/{STAGE1}",
    )

    parked_rel = f"derivative-attempts/ds999999-{STAGE1}-attempt-1"
    assert (campaign / parked_rel).is_dir(), f"retired derivative not at {parked_rel}"
    assert not (study_ds / "derivatives" / STAGE1).exists(), (
        "retired derivative still present in the study"
    )
    assert f"derivatives/{STAGE1}" not in (study_ds / ".gitmodules").read_text(), (
        "study still registers the retired derivative"
    )
    # It is the SAME dataset relocated, not a copy: the datalad-id survives, so any
    # provenance pointing at it still resolves.
    assert (
        _git(campaign, "config", "-f", ".gitmodules",
             "--get", f"submodule.{parked_rel}.datalad-id").strip() == deriv_id
    ), "retired derivative lost its datalad-id in the move"

    # the ledger cell is reset by the same transition -> iterate would re-scaffold it
    row = _ledger_row(campaign)
    assert not row[f"{STAGE1}_babs"] and not row[f"{STAGE1}_babs-merged"], (
        f"retire did not reset the ledger cell (row={row})"
    )

    _assert_nest_clean([study_ds, campaign], "retire")
    assert (
        _git(campaign, "log", "--first-parent", "-1", "--format=%s")
        .strip()
        .startswith(f"retire ds999999/{STAGE1}")
    ), "campaign mainline did not record the retire node"


def test_chained_run(campaign, cluster_config, rawdata, study):
    """Two simbids stages where stage2 consumes stage1's output by name (issue #72).

    Exercises the chaining path unit tests can't reach: the scaffold gate (stage2
    must wait for stage1 to merge), the output-RIA injection into stage2's babs
    config, and babs cloning stage1's output as stage2's input. Both stages run the
    full scaffold->submit->merge flow; --limit 1 on stage1 propagates through the
    chain (stage2's babs-derived job set intersects to stage1's one produced subject).
    """
    _venv_run(
        campaign,
        "mechababs",
        "configure",
        "--pipelines",
        f"code/mechababs/examples/pipelines/{STAGE1}.yaml,code/mechababs/examples/pipelines/{STAGE2}.yaml",
        "--cluster",
        f"code/mechababs/examples/clusters/{cluster_config}",
        "--limit",
        "1",
    )
    _venv_run(
        campaign,
        "mechababs",
        "add-dataset",
        str(rawdata),
        "--study",
        str(study),
        "--processing-level",
        "subject",
    )
    study_ds = campaign / "studies" / "study-ds999999"

    # --- tick with batch covering both cells: stage1 scaffolds; stage2 is GATED
    #     (stage1 not merged) so it must NOT scaffold this tick ---
    _venv_run(campaign, "mechababs", "iterate", "--batch", "2")
    row = _ledger_row(campaign)
    assert row[f"{STAGE1}_babs"], "stage1 did not scaffold"
    assert not row.get(f"{STAGE2}_babs"), (
        "stage2 scaffolded before stage1 merged — the chain gate failed"
    )

    # --- drive stage1 to merged (submit -> wait -> merge); stage2 stays gated ---
    stage1_proj = campaign / row[f"{STAGE1}_babs"]
    _venv_run(
        campaign, "mechababs", "iterate", "--batch", "1"
    )  # active stage1 -> submit
    _venv_run(
        campaign, "babs", "status", "--wait", "--wait-interval", "5", str(stage1_proj)
    )
    _venv_run(
        campaign, "mechababs", "iterate", "--batch", "1"
    )  # active stage1 -> merge
    row = _ledger_row(campaign)
    assert row[f"{STAGE1}_babs-merged"] == "true", "stage1 did not merge"

    # --- gate now open: stage2 scaffolds, with stage1's output-RIA injected ---
    _venv_run(campaign, "mechababs", "iterate", "--batch", "1")  # scaffold stage2
    row = _ledger_row(campaign)
    assert row[f"{STAGE2}_babs"], "stage2 did not scaffold after stage1 merged"
    stage2_proj = campaign / row[f"{STAGE2}_babs"]

    # stage2's committed babs config carries stage1's output-RIA as the chained
    # input's origin_url — the alias form, pointing at stage1's project.
    babs_cfg = yaml.safe_load(
        (stage2_proj / ".babs" / "babs_init_config.yaml").read_text()
    )
    origin = babs_cfg["input_datasets"][STAGE1]["origin_url"]
    assert origin.startswith("ria+file://") and origin.endswith("output_ria#~data"), (
        f"stage2's chained-input origin_url is not the stage1 output-RIA alias: {origin}"
    )
    assert f"derivatives/{STAGE1}/.babs/output_ria" in origin, (
        f"origin_url does not point at stage1's output RIA: {origin}"
    )
    # babs cloned stage1's output as stage2's input subdataset (the RIA resolved).
    assert f"sourcedata/{STAGE1}" in (stage2_proj / ".gitmodules").read_text(), (
        "stage2 did not register stage1's output as an input subdataset"
    )

    # --- drive stage2 to merged; the chain completes end to end ---
    _venv_run(
        campaign, "mechababs", "iterate", "--batch", "1"
    )  # active stage2 -> submit
    _venv_run(
        campaign, "babs", "status", "--wait", "--wait-interval", "5", str(stage2_proj)
    )
    _venv_run(
        campaign, "mechababs", "iterate", "--batch", "1"
    )  # active stage2 -> merge
    row = _ledger_row(campaign)
    assert row[f"{STAGE2}_babs-merged"] == "true", f"stage2 did not merge (row={row})"
    _assert_nest_clean([stage2_proj, study_ds, campaign], "chained merge")
