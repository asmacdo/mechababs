"""Tests for `run_in_podman.sh`'s interrupt handling (con/mechababs#105).

The failure mode these guard: the interrupt never reaches the workload, so the e2e
runs to completion no matter how many times you press Ctrl-C. Nothing else can catch
that — no test fails, the run just refuses to stop — so it needs a test that actually
interrupts a run.

These drive the REAL script against a stub `podman` on PATH, so they are hermetic and take
about a second: no image, no container, no e2e. What keeps that honest is that the stub
**ignores SIGINT** exactly as the real client does under `podman machine`, so a pass cannot
be an artifact of the stub politely dying on its own.
"""

import os
import signal
import subprocess
import time

import pytest

SCRIPT = "mechababs/testing/e2e/run_in_podman.sh"

# Records one line per call, in order, plus a marker for the signals it is sent.
#
# `run` stands in for a long e2e and is faithful in the three ways the fix depends on: it
# IGNORES SIGINT (the whole reason #105 happened), it dies on SIGTERM, and it lives exactly
# as long as its container — a file under PODMAN_STATE stands in for the container, so
# removing the container ends the client just as it does for real. Without that last part
# the two halves of the teardown could not be told apart.
STUB_PODMAN = """#!/usr/bin/env bash
echo "$*" >> "$PODMAN_CALLS"
name=""
case "$1" in
    run)
        prev=""
        for a in "$@"; do [ "$prev" = "--name" ] && name="$a"; prev="$a"; done
        trap '' INT
        trap 'echo "client-terminated" >> "$PODMAN_CALLS"; exit 143' TERM
        : > "$PODMAN_STATE/$name"
        # `sleep &` + `wait`, never a foreground sleep: bash defers a trap until a
        # foreground command returns, which is the very trap #105 was about.
        while [ -e "$PODMAN_STATE/$name" ]; do sleep 0.1 & wait $!; done
        echo "container-gone" >> "$PODMAN_CALLS"
        ;;
    rm|kill|stop)
        for a in "$@"; do name="$a"; done   # the container is the last argument
        rm -f "$PODMAN_STATE/$name"
        ;;
esac
exit 0
"""


@pytest.fixture
def fake_checkout(tmp_path):
    """A minimal committed checkout with the real script at its real relative path.

    The script derives the repo root from its own location and refuses a dirty tree or a
    detached HEAD, so it needs a real git repo — but not *this* one, whose tree is dirty
    exactly when someone is working on the script.
    """
    repo = tmp_path / "checkout"
    (repo / "mechababs" / "testing" / "e2e").mkdir(parents=True)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, SCRIPT), "rb") as src:
        (repo / SCRIPT).write_bytes(src.read())
    (repo / SCRIPT).chmod(0o755)

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "script")
    return repo


@pytest.fixture
def interrupted_run(tmp_path, fake_checkout):
    """Start the script under a stub podman, Ctrl-C it, and report what it did.

    SIGINT goes to the whole process group because that is what a terminal does, and the
    bug was precisely about which member of that group acts on it. The child gets its own
    session — never a shell's `&`, which would set SIGINT to SIG_IGN and so silently
    disable the very trap under test.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "podman").write_text(STUB_PODMAN)
    (bindir / "podman").chmod(0o755)
    calls_file = tmp_path / "calls.txt"
    calls_file.touch()
    state = tmp_path / "state"
    state.mkdir()

    def calls():
        return calls_file.read_text().splitlines()

    def run():
        proc = subprocess.Popen(
            [str(fake_checkout / SCRIPT)],
            cwd=str(fake_checkout),
            env={
                **os.environ,
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "PODMAN_CALLS": str(calls_file),
                "PODMAN_STATE": str(state),
                "MECHABABS_E2E_WORKDIR": str(tmp_path / "work"),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

        # Interrupt only once the run is genuinely under way, which is the case the bug
        # was about (an interrupt before that has nothing to stop).
        if not _wait_until(lambda: any(c.startswith("run ") for c in calls()), proc):
            proc.kill()
            pytest.fail(f"stub podman was never asked to run:\n{proc.communicate()[0]}")

        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        try:
            output = proc.communicate(timeout=20)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail(
                "script did not exit within 20s of SIGINT — the interrupt was ignored"
            )

        # The stub's own records can land microseconds after the script exits. Short
        # timeout: these are local file writes, so anything slower means they never came.
        _wait_until(
            lambda: "client-terminated" in calls() and _removals(calls()),
            None,
            timeout=5,
        )
        return proc.returncode, output, calls()

    return run


def _wait_until(predicate, proc, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        if proc is not None and proc.poll() is not None:
            return predicate()
        time.sleep(0.05)
    return False


def _removals(calls):
    return [c for c in calls if c.startswith(("rm ", "kill ", "stop "))]


def _summary(calls):
    """The calls, minus the 20-line `podman run` argv that would bury the failure."""
    return "\n".join(c if not c.startswith("run ") else "run …" for c in calls)


def _container_name(calls):
    """The name the script gave `podman run --name`."""
    fields = next(c for c in calls if c.startswith("run ")).split()
    return fields[fields.index("--name") + 1]


def test_interrupt_ends_the_run(interrupted_run):
    """#105 itself: Ctrl-C ends the run instead of being swallowed.

    The stub's `run` ignores SIGINT and otherwise runs until its container goes away, so
    an exit here can only come from the script's own handler. 130 is the
    interrupted-by-SIGINT convention.
    """
    rc, output, _ = interrupted_run()
    assert rc == 130, f"expected 130 (SIGINT), got {rc}\n{output}"
    assert "interrupted" in output, f"no interrupt notice printed:\n{output}"


def test_interrupt_removes_the_container_it_started(interrupted_run):
    """Teardown must name the container the script actually started.

    Removing the container is what stops the work: pytest, babs, and the inner singularity
    job all live inside it, and no signal the host sends reaches them.
    """
    _, output, calls = interrupted_run()
    name = _container_name(calls)
    assert any(name in c for c in _removals(calls)), (
        f"container {name} was never torn down; podman calls were:\n" + _summary(calls)
    )


def test_interrupt_stops_the_client_too(interrupted_run):
    """Covers an interrupt in the window before the container exists.

    Tearing it down by name hits nothing then, and bash does not reap the backgrounded
    client on exit — so the client would go on to create and run the container with
    nobody watching. Stopping the client is what closes that window.
    """
    _, output, calls = interrupted_run()
    assert "client-terminated" in calls, (
        "the podman client was left running, so an interrupt before the container exists "
        "would still start it; podman calls were:\n" + _summary(calls)
    )
