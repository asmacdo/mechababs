"""The `datalad run` dispatch wrapper.

The declaration half (what a verb's argv and outputs are) is pure and tested as
such. The mechanics half really runs `datalad run` against a real dataset — the
things worth asserting (explicit mode, the run record actually landing, the
recorded pwd) exist nowhere but in datalad's behaviour, so a mocked version would
assert only that the code calls the function it calls.

The command dispatched in those tests is a trivial writer rather than
`mechababs-inner`: this is the wrapper under test, and a real scaffold needs a
real babs and a real container, which is the e2e's job.
"""

import json
import os
import shlex
import subprocess

import pytest

from mechababs import campaign as campaign_mod
from mechababs import dispatch

LABEL = "e2e"
ANCHOR = "bids-app-configs/SimBIDS-0.0.3+anchor.yaml"
SOURCEDATA = "sourcedata/ds999999"


# --------------------------------------------------------------------------
# The declaration: what a verb's command and outputs are
# --------------------------------------------------------------------------


def test_the_recorded_command_names_the_campaign_and_is_relative():
    """No absolute paths and no reliance on MECHABABS_CAMPAIGN — a run record has
    to re-execute somewhere else, under someone else's environment."""
    cmd = dispatch.inner_command("scaffold", LABEL, SOURCEDATA, ANCHOR)
    assert cmd == [
        "mechababs-inner",
        "scaffold",
        "--campaign",
        LABEL,
        "--source-dataset",
        SOURCEDATA,
        "--app",
        ANCHOR,
    ]
    assert not any(str(part).startswith("/") for part in cmd), cmd


def test_scaffold_declares_the_four_paths_it_writes(tmp_path):
    """Explicit mode captures only what is declared, so the declaration is the
    contract — including `.gitmodules`, which datalad commits mid-command when it
    registers the new derivative as a subdataset."""
    outputs = dispatch.scaffold_outputs(tmp_path, LABEL, SOURCEDATA, ANCHOR)
    assert outputs == [
        "derivatives/SimBIDS-0.0.3+anchor+ds999999+e2e",
        ".mechababs/campaigns/e2e/sourcedata+derivatives.tsv",
        (
            ".mechababs/campaigns/e2e/inclusions/"
            "sourcedata-ds999999_SimBIDS-0.0.3+anchor.csv"
        ),
        ".gitmodules",
    ]
    assert all(not os.path.isabs(o) for o in outputs), outputs


def test_the_message_says_which_cell_advanced_and_where(tmp_path):
    assert dispatch.scaffold_message(SOURCEDATA, ANCHOR, LABEL) == (
        "mechababs scaffold sourcedata/ds999999 SimBIDS-0.0.3+anchor -> "
        "derivatives/SimBIDS-0.0.3+anchor+ds999999+e2e"
    )


# --------------------------------------------------------------------------
# The mechanics: a real `datalad run` against a real dataset
# --------------------------------------------------------------------------


@pytest.fixture
def dataset(tmp_path):
    """A clean datalad dataset standing in for a study."""
    study = tmp_path / "study-ds999999"
    subprocess.run(["datalad", "create", "-c", "text2git", str(study)], check=True)
    return study


def _run_record(study):
    """The JSON datalad embeds in the run commit's message body."""
    body = subprocess.run(
        ["git", "-C", str(study), "log", "-1", "--format=%b"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    start = body.index("{")
    return json.loads(body[start : body.rindex("}") + 1])


WRITE = [
    "python",
    "-c",
    "import pathlib; pathlib.Path('landed.txt').write_text('x\\n')",
]


def test_a_dispatch_lands_as_a_run_record_with_the_command_verbatim(dataset):
    dispatch.dispatch(dataset, WRITE, outputs=["landed.txt"], message="write it")

    assert (dataset / "landed.txt").is_file()
    assert dispatch.head_subject(dataset) == "[DATALAD RUNCMD] write it"
    record = _run_record(dataset)
    # datalad stores the argv shell-quoted, so the record reads as a command line.
    assert shlex.split(record["cmd"]) == WRITE, record["cmd"]
    assert record["outputs"] == ["landed.txt"]
    assert not subprocess.run(
        ["git", "-C", str(dataset), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip(), "the dispatch left the study dirty"


def test_the_run_is_recorded_study_relative_even_when_dispatched_from_elsewhere(
    dataset, tmp_path, monkeypatch
):
    """The both-levels lens: at a superstudy the reconciler stands at the super and
    dispatches into a member, so `study` is a parameter and never the cwd. The
    recorded `pwd` still has to be the study's own, or the record re-executes
    against whatever directory the rerunner happens to be in.
    """
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    dispatch.dispatch(dataset, WRITE, outputs=["landed.txt"], message="from afar")

    record = _run_record(dataset)
    assert record["pwd"] == ".", record
    assert (dataset / "landed.txt").is_file()
    assert not (elsewhere / "landed.txt").exists(), "the command ran in the wrong cwd"


def test_an_undeclared_output_is_not_captured_by_the_run(dataset):
    """Explicit mode's cost, asserted so it is a known one: what is not declared is
    not recorded. This is exactly why the clean check runs first."""
    cmd = [
        "python",
        "-c",
        (
            "import pathlib;"
            "pathlib.Path('landed.txt').write_text('x\\n');"
            "pathlib.Path('undeclared.txt').write_text('y\\n')"
        ),
    ]
    dispatch.dispatch(dataset, cmd, outputs=["landed.txt"], message="partial")

    tracked = subprocess.run(
        ["git", "-C", str(dataset), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert "landed.txt" in tracked
    assert "undeclared.txt" not in tracked


def test_a_dirty_study_refuses_the_dispatch_before_anything_runs(dataset):
    (dataset / "someone-elses-work.txt").write_text("mine\n")
    with pytest.raises(RuntimeError, match="someone-elses-work.txt"):
        dispatch.dispatch(dataset, WRITE, outputs=["landed.txt"], message="nope")
    assert not (dataset / "landed.txt").exists(), "the refused dispatch still ran"


def test_dry_run_prints_the_dispatch_and_changes_nothing(dataset, capsys):
    dispatch.dispatch(
        dataset, WRITE, outputs=["landed.txt"], message="planned", dry_run=True
    )
    assert "DRY-RUN" in capsys.readouterr().err
    assert not (dataset / "landed.txt").exists()


def test_the_runcmd_backstop_fires_when_no_record_landed(dataset):
    """A command that makes its own commits can leave a run with nothing of its own
    to save, and older datalad dropped the record entirely — a silent failure, so
    it is checked rather than assumed."""
    subprocess.run(
        ["git", "-C", str(dataset), "commit", "--allow-empty", "-qm", "a plain commit"],
        check=True,
    )
    with pytest.raises(RuntimeError, match="not a run record"):
        dispatch.require_runcmd_head(dataset, "something")


def test_a_command_that_commits_for_itself_still_lands_a_run_record(dataset):
    """The scaffold shape in miniature: `babs init` commits inside the study before
    datalad's own save gets a turn. datalad >= 1.6 keeps the record; the pyproject
    floor says so and this says it is in force."""
    cmd = [
        "python",
        "-c",
        (
            "import pathlib, subprocess;"
            "pathlib.Path('landed.txt').write_text('x\\n');"
            "subprocess.run(['git','add','landed.txt'], check=True);"
            "subprocess.run(['git','commit','-qm','inner commit'], check=True)"
        ),
    ]
    dispatch.dispatch(dataset, cmd, outputs=["landed.txt"], message="inner-commits")
    assert dispatch.head_subject(dataset) == "[DATALAD RUNCMD] inner-commits"


def test_an_undeclared_path_in_an_inner_commit_is_refused(dataset):
    """datalad's `dirty-committed` guard, left armed on purpose: a mid-command
    commit that sweeps in an undeclared path would lose that path's provenance.

    Scaffold answers it by declaring `.gitmodules` — the path datalad itself
    commits when it registers the new derivative — rather than muting the guard.
    """
    cmd = [
        "python",
        "-c",
        (
            "import pathlib, subprocess;"
            "pathlib.Path('landed.txt').write_text('x\\n');"
            "pathlib.Path('smuggled.txt').write_text('y\\n');"
            "subprocess.run(['git','add','.'], check=True);"
            "subprocess.run(['git','commit','-qm','inner commit'], check=True)"
        ),
    ]
    with pytest.raises(subprocess.CalledProcessError):
        dispatch.dispatch(dataset, cmd, outputs=["landed.txt"], message="smuggler")


def test_merge_declares_the_two_paths_it_writes(tmp_path):
    """No `.gitmodules`: at the study level merge registers and drops nothing. The
    derivative was registered at scaffold; merge only moves its HEAD."""
    outputs = dispatch.merge_outputs(tmp_path, LABEL, SOURCEDATA, ANCHOR)
    assert outputs == [
        "derivatives/SimBIDS-0.0.3+anchor+ds999999+e2e",
        ".mechababs/campaigns/e2e/sourcedata+derivatives.tsv",
    ]
    assert dispatch.GITMODULES not in outputs
    assert all(not os.path.isabs(o) for o in outputs), outputs


def test_merge_declares_no_inclusion_pin(tmp_path):
    """Scaffold writes the pin; merge must not declare it, or a stray edit to it
    would be swept into the merge's record."""
    pin = dispatch.scaffold_outputs(tmp_path, LABEL, SOURCEDATA, ANCHOR)[2]
    assert pin not in dispatch.merge_outputs(tmp_path, LABEL, SOURCEDATA, ANCHOR)


def test_the_merge_message_names_the_cell(tmp_path):
    assert dispatch.merge_message(SOURCEDATA, ANCHOR) == (
        "mechababs merge sourcedata/ds999999 SimBIDS-0.0.3+anchor"
    )


# --------------------------------------------------------------------------
# The plain path: a verb that changes nothing, and the check that says so
# --------------------------------------------------------------------------


def test_submit_declares_nothing_and_names_the_cell(tmp_path):
    """There is no `submit_outputs` to test, and that absence is the design: submit
    writes nothing the study tracks, so it has nothing to declare."""
    assert not hasattr(dispatch, "submit_outputs")
    assert dispatch.submit_message(SOURCEDATA, ANCHOR) == (
        "mechababs submit sourcedata/ds999999 SimBIDS-0.0.3+anchor"
    )


def test_an_unrecorded_verb_resolves_the_inner_cli_absolutely(tmp_path):
    """The bare `mechababs-inner` is an exception forced by provenance — a run
    record has to re-execute elsewhere. Nothing forces it on a verb that is not
    recorded, so that one is resolved beside sys.prefix like every other shell-out.
    """
    recorded = dispatch.inner_command("scaffold", LABEL, SOURCEDATA, ANCHOR)
    assert recorded[0] == dispatch.INNER
    unrecorded = dispatch.inner_command(
        "submit", LABEL, SOURCEDATA, ANCHOR, executable=dispatch.inner_bin()
    )
    assert os.path.isabs(unrecorded[0])
    assert os.path.basename(unrecorded[0]) == dispatch.INNER
    # Same argv shape either way — only argv[0] and the verb differ.
    assert recorded[2:] == unrecorded[2:]


def test_a_plain_run_leaves_no_record_and_no_commit(dataset):
    """The whole point of the plain path: the verb runs, and the study's history is
    exactly as it was."""
    head = dispatch.head_sha(dataset)
    dispatch.plain(dataset, ["true"], message="deployed some jobs")
    assert dispatch.head_sha(dataset) == head, "a plain verb committed"


def test_a_plain_verb_that_dirties_the_study_is_caught(dataset):
    """The determination "this verb changes nothing tracked" is about a tool we do
    not own, so it is re-checked every run instead of trusted once."""
    with pytest.raises(RuntimeError, match="changed .* tracked state"):
        dispatch.plain(dataset, WRITE, message="a submit that wrote something")


def test_a_plain_verb_that_commits_for_itself_is_caught(dataset):
    """The other half: clean afterwards, but a bare commit with no command in it."""
    cmd = [
        "python",
        "-c",
        (
            "import pathlib, subprocess;"
            "pathlib.Path('landed.txt').write_text('x\\n');"
            "subprocess.run(['git','add','landed.txt'], check=True);"
            "subprocess.run(['git','commit','-qm','snuck one in'], check=True)"
        ),
    ]
    with pytest.raises(RuntimeError, match="HEAD moved"):
        dispatch.plain(dataset, cmd, message="a submit that committed")


def test_a_dirty_study_refuses_a_plain_verb_too(dataset):
    """Clean in, so the after-check can attribute what it finds to the verb."""
    (dataset / "someone-elses-work.txt").write_text("mine\n")
    with pytest.raises(RuntimeError, match="someone-elses-work.txt"):
        dispatch.plain(dataset, WRITE, message="nope")
    assert not (dataset / "landed.txt").exists(), "the refused verb still ran"


def test_dry_run_prints_the_plain_verb_and_changes_nothing(dataset, capsys):
    dispatch.plain(dataset, WRITE, message="planned", dry_run=True)
    assert "DRY-RUN" in capsys.readouterr().err
    assert not (dataset / "landed.txt").exists()


def test_the_statefile_path_comes_from_the_campaign_module(tmp_path):
    """The declaration is derived, never spelled out twice — a renamed statefile
    would otherwise be declared under its old name and silently not captured."""
    outputs = dispatch.scaffold_outputs(tmp_path, LABEL, SOURCEDATA, ANCHOR)
    assert campaign_mod.STATE_FILENAME in outputs[1]
