"""dispatch.py — running an action verb, inside a `datalad run` when it changes the study.

A transition that changes the study is not saved with a hand-written label; it is
**dispatched**, so what lands in git is the verbatim command that produced the
change. That is the orchestration provenance: the study's history is a list of
re-executable steps, not a list of adjectives.

**Only the change-making verbs are wrapped.** `scaffold` and `merge` write into the
study, so they go through `dispatch`. `submit` only sbatches — babs's job
bookkeeping is gitignored inside the derivative — so it goes through `plain`, which
runs the same verb with no run record because there is no change to record. Wrapping
it anyway would put an empty node in the study's history for every iterate that deploys
jobs, which is noise pretending to be provenance. `plain` re-checks the study
afterwards, so the claim is enforced rather than asserted.

Three mechanics, each load-bearing:

**`-d .` with cwd inside the study.** The run is recorded at the study, where the
derivative and the statefile live, and a study-relative `pwd` is what lets the
record re-execute somewhere else. At a superstudy the reconciler dispatches one
study-level run per member — the `-d` is always a (lone or member) study, so runs
never nest at the same level.

**`--explicit`, with every output declared.** Without it datalad discovers what
changed by walking the dataset, and a study's `sourcedata/` is exactly the walk
that must not happen once per cell. Explicit mode makes the declaration the
contract instead. Its cost is that an undeclared side-write is silently left
behind, which is why `require_clean_shallow` runs first: whatever is uncommitted
before the run did not come from mechababs.

**`.gitmodules` among scaffold's outputs.** `babs init` creates the derivative as
a datalad dataset inside the study, and datalad registers it in the parent
mid-command — a commit datalad's `datalad.run.dirty-committed=error` guard would
otherwise refuse as touching undeclared paths. Declaring `.gitmodules` satisfies
the guard while leaving it armed for a genuinely unexpected mid-command commit,
which is the point of keeping it on.
"""

import subprocess
import sys
from pathlib import Path

from mechababs import campaign as campaign_mod
from mechababs import scaffold as scaffold_mod
from mechababs.utils import require_clean_shallow, shallow_status

# The hidden action CLI the run records. A bare name, resolved on PATH: inside a
# campaign that is the campaign venv's, which is the pinned mechababs — and a
# recorded command naming an absolute interpreter would not re-execute anywhere
# else.
INNER = "mechababs-inner"

# datalad from THIS environment rather than PATH, for the same reason every other
# shell-out in the package does it: the environment is the campaign's pin.
DATALAD = str(Path(sys.prefix) / "bin" / "datalad")

RUNCMD_PREFIX = "[DATALAD RUNCMD]"

# `.gitmodules` is on every verb's list that can register or drop a subdataset.
GITMODULES = ".gitmodules"


def inner_bin():
    """This environment's `mechababs-inner`, for a verb that is NOT being recorded.

    The bare `INNER` above is a deliberate exception, forced by provenance: an
    absolute path in a run record would not re-execute anywhere else. Nothing forces
    it on an unrecorded verb, so that one resolves beside `sys.prefix` like every
    other shell-out in the package, and cannot be answered by a stray install on
    PATH.
    """
    return str(Path(sys.prefix) / "bin" / INNER)


def inner_command(verb, label, source_dataset, app_config, *, executable=INNER):
    """The argv of the action verb — the thing the run record will hold verbatim.

    Every identifier here is study- or campaign-relative, and the campaign label is
    a flag rather than the `MECHABABS_CAMPAIGN` env var: a `datalad rerun` must not
    depend on the ambient environment of whoever reruns it. `executable` defaults to
    the bare, recordable name; an unrecorded verb passes `inner_bin()`.
    """
    return [
        executable,
        verb,
        "--campaign",
        label,
        "--source-dataset",
        source_dataset,
        "--app",
        app_config,
    ]


def scaffold_outputs(study, label, source_dataset, app_config):
    """What a scaffold writes, study-relative — its `--output` declaration.

    Four things, and the run captures exactly these: the derivative babs inits, the
    statefile row recording it, the inclusion pinned beside that statefile, and the
    `.gitmodules` entry that registers the derivative as a subdataset.
    """
    pin = scaffold_mod.inclusion_pin(study, label, source_dataset, app_config)
    return [
        scaffold_mod.derivative_path(source_dataset, app_config, label),
        str(campaign_mod.state_path(study, label).relative_to(study)),
        str(pin.relative_to(study)),
        GITMODULES,
    ]


def scaffold_message(source_dataset, app_config, label):
    return (
        f"mechababs scaffold {source_dataset} "
        f"{scaffold_mod.app_stem(app_config)} -> "
        f"{scaffold_mod.derivative_path(source_dataset, app_config, label)}"
    )


def merge_outputs(study, label, source_dataset, app_config):
    """What a merge writes, study-relative — its `--output` declaration.

    Two things, and deliberately not `.gitmodules`: at the study level merge
    registers and drops nothing. The derivative was registered as a subdataset at
    scaffold; merge only moves its HEAD, so the study's diff is that gitlink plus
    the statefile row. (The merged branch may well change the DERIVATIVE's own
    `.gitmodules` — that is inside the derivative, and declaring the derivative
    covers it.)
    """
    return [
        scaffold_mod.derivative_path(source_dataset, app_config, label),
        str(campaign_mod.state_path(study, label).relative_to(study)),
    ]


def merge_message(source_dataset, app_config):
    return f"mechababs merge {source_dataset} {scaffold_mod.app_stem(app_config)}"


def submit_message(source_dataset, app_config):
    """What a submit prints. There is no `submit_outputs`, and that is the statement:
    submit declares nothing because it writes nothing the study tracks."""
    return f"mechababs submit {source_dataset} {scaffold_mod.app_stem(app_config)}"


def head_subject(study):
    return _git_log(study, "%s")


def head_sha(study):
    return _git_log(study, "%H")


def _git_log(study, fmt):
    return subprocess.run(
        ["git", "-C", str(study), "log", "-1", f"--format={fmt}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def require_runcmd_head(study, message):
    """Assert the dispatch actually produced a run record. The loud backstop.

    A `datalad run` whose command makes its own commits can end up saving nothing
    of its own, and older datalad dropped the run record entirely in that case —
    leaving a study that looks advanced with no re-executable command in it. That
    failure is silent by nature, so it is checked rather than assumed. (The
    `datalad>=1.6` floor in pyproject.toml is the fix; this is the check that it
    is in force.)
    """
    subject = head_subject(study)
    if not subject.startswith(RUNCMD_PREFIX):
        raise RuntimeError(
            f"{study} HEAD is not a run record after dispatching {message!r}.\n"
            f"  HEAD: {subject}\n"
            "The transition was meant to land as a re-executable command; without "
            "the run record the study's history says what changed but not how."
        )


def dispatch(study, cmd, *, outputs, message, dry_run=False):
    """Run ``cmd`` at ``study`` under ``datalad run --explicit``, declaring ``outputs``.

    ``study`` is the run's dataset AND its working directory, so the recorded
    command is study-relative regardless of where the caller stands.
    """
    study = Path(study)
    require_clean_shallow(study, what=f"dispatching: {message}")

    argv = [DATALAD, "run", "--explicit", "-d", ".", "-m", message]
    for output in outputs:
        argv += ["--output", output]
    argv += ["--", *[str(c) for c in cmd]]

    if dry_run:
        print(f"DRY-RUN  {' '.join(argv)}   (cwd={study})", file=sys.stderr)
        return
    print(f"+ {' '.join(argv)}   (cwd={study})", file=sys.stderr)
    subprocess.run(argv, cwd=str(study), check=True)
    require_runcmd_head(study, message)


def require_unchanged(study, head, *, what):
    """Assert a plain verb really left the study's tracked state alone.

    The determination that a verb needs no `datalad run` is a claim about a tool we
    do not own, so it is checked on every run rather than trusted once. Both halves
    matter: a verb could dirty the tree (uncommitted, uncaptured) or commit for
    itself (captured, but as a bare commit with no command in it).
    """
    dirty = shallow_status(study)
    moved = head_sha(study) != head
    if not dirty and not moved:
        return
    raise RuntimeError(
        f"{what} changed {study}'s tracked state, and it was run without a "
        "`datalad run` because it is not supposed to.\n"
        + (f"  HEAD moved: {head} -> {head_sha(study)}\n" if moved else "")
        + "".join(f"  {line}\n" for line in dirty)
        + "That change is now in the study with no command recorded against it. "
        "The verb needs dispatching, with what it writes declared as outputs."
    )


def plain(study, cmd, *, message, dry_run=False):
    """Run ``cmd`` at ``study`` with NO run record — for a verb that changes nothing.

    Same shape as `dispatch`, minus the wrapper: clean in, the verb, and then the
    check that it stayed clean. `require_clean_shallow` runs first for the same
    reason it does there — so the after-check can attribute what it finds.
    """
    study = Path(study)
    require_clean_shallow(study, what=f"running: {message}")
    argv = [str(c) for c in cmd]

    if dry_run:
        print(f"DRY-RUN  {' '.join(argv)}   (cwd={study})", file=sys.stderr)
        return
    head = head_sha(study)
    print(f"+ {' '.join(argv)}   (cwd={study})", file=sys.stderr)
    subprocess.run(argv, cwd=str(study), check=True)
    require_unchanged(study, head, what=message)


def scaffold(study, label, source_dataset, app_config, *, dry_run=False):
    """Dispatch the scaffold transition for one cell."""
    dispatch(
        study,
        inner_command("scaffold", label, source_dataset, app_config),
        outputs=scaffold_outputs(study, label, source_dataset, app_config),
        message=scaffold_message(source_dataset, app_config, label),
        dry_run=dry_run,
    )


def submit(study, label, source_dataset, app_config, *, dry_run=False):
    """Run the submit transition for one cell — plainly, with no run record."""
    plain(
        study,
        inner_command(
            "submit", label, source_dataset, app_config, executable=inner_bin()
        ),
        message=submit_message(source_dataset, app_config),
        dry_run=dry_run,
    )


def merge(study, label, source_dataset, app_config, *, dry_run=False):
    """Dispatch the merge transition for one cell."""
    dispatch(
        study,
        inner_command("merge", label, source_dataset, app_config),
        outputs=merge_outputs(study, label, source_dataset, app_config),
        message=merge_message(source_dataset, app_config),
        dry_run=dry_run,
    )
