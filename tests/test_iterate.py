"""The reconciler tick: the routing table, the gate, the batch, and the flock.

Everything the tick *dispatches* is stubbed — a real transition needs a real babs, a
real scheduler and a real container, which is the e2e's job. What is mechababs' here
is the decision: given a shard and (for an active cell) a set of live counts, which
verb does each cell get, and which cells get nothing at all.

The stubs mutate the shard the way the real verbs do (scaffold records `babs`, merge
sets `merged`), because the tick re-reads it between cells — so a stub that only
recorded the call would make the multi-cell cases lie.
"""

from pathlib import Path

import pytest

from mechababs import campaign as campaign_mod
from mechababs import iterate as iterate_mod
from mechababs import scaffold as scaffold_mod

LABEL = "e2e"
ANCHOR = "bids-app-configs/SimBIDS-0.0.3+anchor.yaml"
CHAIN = "bids-app-configs/SimBIDS-0.0.3+chain.yaml"
SOURCEDATA = "sourcedata/ds999999"
ANCHOR_PROJECT = "derivatives/SimBIDS-0.0.3+anchor+ds999999"

ALL_DONE = {"total": 2, "submitted": 2, "done": 2, "failed": 0}
STILL_RUNNING = {"total": 2, "submitted": 2, "done": 1, "failed": 0}
SOME_FAILED = {"total": 2, "submitted": 2, "done": 1, "failed": 1}
UNSUBMITTED = {"total": 2, "submitted": 0, "done": 0, "failed": 0}

IDENTITY = {"processing_level": "subject", "n_subjects": "2", "n_sessions": ""}


def cell(app_config, *, depends_on="", babs="", merged=""):
    return {
        "source_dataset": SOURCEDATA,
        "app_config": app_config,
        "depends_on": depends_on,
        "babs": babs,
        "merged": merged,
        **IDENTITY,
    }


def write(study, rows):
    campaign_mod.write_state(study, LABEL, rows)


@pytest.fixture
def study(tmp_path):
    """A study with a two-cell shard: an anchor, and a chain that depends on it.

    Deliberately NOT the cwd — see `test_the_tick_never_assumes_it_is_standing_in_the_study`.
    """
    study = tmp_path / "study-ds999999"
    campaign_mod.campaign_dir(study, LABEL).mkdir(parents=True)
    campaign_mod.state_path(study, LABEL).write_text(campaign_mod.initial_header())
    write(study, [cell(ANCHOR), cell(CHAIN, depends_on=ANCHOR)])
    return study


class _Tick(list):
    """The transitions the tick dispatched, in order, plus the knobs the stubs read.

    `status` is what `babs status` reports for an active cell (one dict for every
    cell, which is enough: no test here needs two cells active at once with
    *different* counts). `cleans` counts the once-per-tick clean check; `locks`
    counts the flock.
    """

    status = None
    cleans = 0
    locks = 0


@pytest.fixture
def tick(monkeypatch, study):
    """Stub the three dispatches, the babs query, the clean check and the flock."""
    calls = _Tick()
    calls.status = dict(ALL_DONE)

    def record(verb, *, column=None, value=""):
        def fake(study_arg, label, source_dataset, app_config, *, dry_run=False):
            calls.append(
                {
                    "verb": verb,
                    "study": str(study_arg),
                    "label": label,
                    "cell": (source_dataset, app_config),
                    "dry_run": dry_run,
                }
            )
            if column and not dry_run:
                # The real verbs write the shard themselves, and the tick re-reads it
                # between cells — so the stub has to move the state too.
                rows = campaign_mod.read_state(study_arg, label)
                row = campaign_mod.find_cell(rows, source_dataset, app_config)
                row[column] = value or scaffold_mod.derivative_path(
                    source_dataset, app_config
                )
                campaign_mod.write_state(study_arg, label, rows)

        return fake

    monkeypatch.setattr(
        iterate_mod.dispatch, "scaffold", record("scaffold", column="babs")
    )
    monkeypatch.setattr(iterate_mod.dispatch, "submit", record("submit"))
    monkeypatch.setattr(
        iterate_mod.dispatch, "merge", record("merge", column="merged", value="true")
    )

    def fake_status(project):
        calls.append({"verb": "babs status", "project": str(project)})
        return dict(calls.status)

    monkeypatch.setattr(iterate_mod.babs_status, "read_status", fake_status)

    def fake_clean(root, *, what="this operation"):
        calls.cleans += 1

    monkeypatch.setattr(iterate_mod, "require_clean_shallow", fake_clean)

    real_flocked = iterate_mod.utils.flocked

    def counting_flock(lock):
        calls.locks += 1
        return real_flocked(lock)

    monkeypatch.setattr(iterate_mod.utils, "flocked", counting_flock)
    return calls


def verbs(calls):
    return [c["verb"] for c in calls]


def dispatched(calls):
    return [c for c in calls if c["verb"] != "babs status"]


# --------------------------------------------------------------------------
# The routing table: which state each set of columns is in
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row, expected",
    [
        (cell(ANCHOR), (iterate_mod.SCAFFOLD, "")),
        (cell(ANCHOR, babs=ANCHOR_PROJECT), (iterate_mod.ACTIVE, ANCHOR_PROJECT)),
        (
            cell(ANCHOR, babs=ANCHOR_PROJECT, merged="true"),
            (iterate_mod.DONE, ""),
        ),
        # Merged wins even with no project recorded: `merged` set is the done state,
        # and done cells are never queried.
        (cell(ANCHOR, merged="true"), (iterate_mod.DONE, "")),
    ],
)
def test_state_is_read_off_the_columns(row, expected):
    """There is no status enum — these are names for what the columns already say."""
    assert iterate_mod.route([row], row) == expected


def test_a_dependent_cell_waits_until_its_producer_is_merged():
    anchor = cell(ANCHOR, babs=ANCHOR_PROJECT)
    chain = cell(CHAIN, depends_on=ANCHOR)
    rows = [anchor, chain]
    assert iterate_mod.route(rows, chain) == (
        iterate_mod.WAITING,
        "SimBIDS-0.0.3+anchor",
    )

    anchor["merged"] = "true"
    assert iterate_mod.route(rows, chain) == (iterate_mod.SCAFFOLD, "")


def test_the_gate_is_a_shard_local_lookup_on_the_same_source_dataset():
    """An edge can never cross studies because the reconciler only looks in the shard
    it is reconciling — and, within it, only at the same source dataset's rows."""
    other = dict(cell(ANCHOR, merged="true"), source_dataset="sourcedata/ds000001")
    chain = cell(CHAIN, depends_on=ANCHOR)
    state, detail = iterate_mod.route([other, chain], chain)
    assert state == iterate_mod.WAITING
    assert "no cell for it in this shard" in detail


# --------------------------------------------------------------------------
# One tick: what each state gets dispatched
# --------------------------------------------------------------------------


def test_a_not_started_cell_is_scaffolded(study, tick):
    iterate_mod.tick(study, LABEL, batch=1)

    (call,) = dispatched(tick)
    assert call["verb"] == "scaffold"
    assert call["cell"] == (SOURCEDATA, ANCHOR)
    assert call["study"] == str(study)
    assert call["label"] == LABEL


def test_a_waiting_cell_is_noted_and_passed_over_not_blocked_on(study, tick, capsys):
    """Gating is noting: the tick says so and moves on, and the next tick re-checks."""
    iterate_mod.tick(study, LABEL)

    assert [c["cell"] for c in dispatched(tick)] == [(SOURCEDATA, ANCHOR)], (
        "the dependent cell was advanced before its producer merged"
    )
    err = capsys.readouterr().err
    assert "waiting on SimBIDS-0.0.3+anchor" in err, err
    assert err.count(iterate_mod.PREFIX) >= 2, "iterate's lines are not distinguishable"


@pytest.mark.parametrize(
    "status, verb",
    [(UNSUBMITTED, "submit"), (ALL_DONE, "merge")],
)
def test_an_active_cells_next_step_comes_from_the_live_counts(
    study, tick, status, verb
):
    write(study, [cell(ANCHOR, babs=ANCHOR_PROJECT)])
    tick.status = dict(status)

    iterate_mod.tick(study, LABEL)

    assert verbs(tick) == ["babs status", verb]
    assert tick[0]["project"] == str(study / ANCHOR_PROJECT)


def test_jobs_still_in_flight_are_left_alone(study, tick):
    write(study, [cell(ANCHOR, babs=ANCHOR_PROJECT)])
    tick.status = dict(STILL_RUNNING)

    assert iterate_mod.tick(study, LABEL) == 0
    assert dispatched(tick) == [], "a running cell was advanced"


def test_a_failed_cell_is_flagged_loudly_and_never_merged(study, tick, capsys):
    """Merging a partial set is silent, not loud — babs merges whatever branches it
    finds — so the reconciler refuses and says so unmistakably."""
    write(study, [cell(ANCHOR, babs=ANCHOR_PROJECT), cell(CHAIN)])
    tick.status = dict(SOME_FAILED)

    iterate_mod.tick(study, LABEL)

    assert "merge" not in verbs(tick), "a failed cell was merged"
    err = capsys.readouterr().err
    assert "FAILED" in err and "NOT merging" in err, err
    assert f"babs status {ANCHOR_PROJECT}" in err, "the flag does not say where to look"


def test_a_failure_does_not_halt_the_tick(study, tick):
    """Level-triggered: one stuck cell must not stop the campaign's other cells."""
    write(study, [cell(ANCHOR, babs=ANCHOR_PROJECT), cell(CHAIN)])
    tick.status = dict(SOME_FAILED)

    iterate_mod.tick(study, LABEL)

    assert [c["cell"] for c in dispatched(tick)] == [(SOURCEDATA, CHAIN)], (
        "the cells after the failed one never got their turn"
    )


def test_a_failure_is_a_per_tick_reading_not_a_column(study, tick):
    """Nothing is written, so a repair-and-resubmit takes effect with nothing to clear:
    the same shard routes to `merge` the moment the counts change."""
    write(study, [cell(ANCHOR, babs=ANCHOR_PROJECT)])
    tick.status = dict(SOME_FAILED)
    before = campaign_mod.state_path(study, LABEL).read_text()

    iterate_mod.tick(study, LABEL)
    assert campaign_mod.state_path(study, LABEL).read_text() == before

    tick.status = dict(ALL_DONE)
    iterate_mod.tick(study, LABEL)
    assert "merge" in verbs(tick)


def test_a_merged_cell_is_skipped_without_asking_babs(study, tick):
    """The economy the `merged` column buys: a done cell costs no query at all."""
    write(study, [cell(ANCHOR, babs=ANCHOR_PROJECT, merged="true")])

    assert iterate_mod.tick(study, LABEL) == 0
    assert tick == [], "a merged cell was queried or advanced"


def test_each_cell_advances_by_at_most_one_transition(study, tick):
    """A not-started cell scaffolds and stops — it does not go on to submit."""
    write(study, [cell(ANCHOR)])
    tick.status = dict(UNSUBMITTED)

    iterate_mod.tick(study, LABEL)

    assert verbs(tick) == ["scaffold"]


def test_a_producer_that_merges_mid_tick_opens_its_dependants_gate(study, tick):
    """The tick re-reads the shard per cell, so ground truth is what routes each one —
    including a change an earlier cell in this same tick made."""
    write(study, [cell(ANCHOR, babs=ANCHOR_PROJECT), cell(CHAIN, depends_on=ANCHOR)])
    tick.status = dict(ALL_DONE)

    iterate_mod.tick(study, LABEL)

    assert [(c["verb"], c["cell"][1]) for c in dispatched(tick)] == [
        ("merge", ANCHOR),
        ("scaffold", CHAIN),
    ]


# --------------------------------------------------------------------------
# Scope: --batch and --derivative
# --------------------------------------------------------------------------


def test_batch_bounds_the_cells_that_advance(study, tick):
    write(study, [cell(ANCHOR), cell(CHAIN)])

    assert iterate_mod.tick(study, LABEL, batch=1) == 1
    assert [c["cell"][1] for c in dispatched(tick)] == [ANCHOR]


def test_a_cell_that_does_not_advance_does_not_consume_batch(study, tick):
    """A done, waiting or still-running cell costs nothing to route, so spending the
    budget on it would make `--batch 1` mean "look at one cell" instead of
    "advance one"."""
    write(
        study,
        [
            cell(ANCHOR, babs=ANCHOR_PROJECT, merged="true"),
            cell(CHAIN, depends_on=ANCHOR + "-missing"),
            cell("bids-app-configs/Third.yaml"),
        ],
    )

    assert iterate_mod.tick(study, LABEL, batch=1) == 1
    assert [c["cell"][1] for c in dispatched(tick)] == ["bids-app-configs/Third.yaml"]


def test_derivative_narrows_to_one_apps_cells(study, tick):
    write(study, [cell(ANCHOR), cell(CHAIN)])

    iterate_mod.tick(study, LABEL, derivative="SimBIDS-0.0.3+chain")

    assert [c["cell"][1] for c in dispatched(tick)] == [CHAIN]


def test_a_derivative_that_matches_nothing_is_a_typo_not_an_empty_tick(study, tick):
    with pytest.raises(SystemExit, match="no cells for --derivative"):
        iterate_mod.tick(study, LABEL, derivative="MRIQC-24.0.2")
    assert tick == []


# --------------------------------------------------------------------------
# The tick's own guarantees
# --------------------------------------------------------------------------


def test_the_flock_is_taken_exactly_once_around_the_whole_tick(study, tick):
    """One lock, held across every cell: the campaign is the single-writer unit. It
    must not be taken per cell (and never inside a verb this tick dispatches — an
    flock is per open-file-description, so that would deadlock against this one)."""
    write(study, [cell(ANCHOR), cell(CHAIN)])

    iterate_mod.tick(study, LABEL)

    assert tick.locks == 1, f"the flock was taken {tick.locks} times"
    assert campaign_mod.flock_path(study, LABEL).exists()


def test_the_clean_check_runs_once_per_tick(study, tick):
    write(study, [cell(ANCHOR), cell(CHAIN)])

    iterate_mod.tick(study, LABEL)

    assert tick.cleans == 1, f"the clean check ran {tick.cleans} times"


def test_the_clean_check_runs_before_anything_is_dispatched(study, monkeypatch, tick):
    """Uncommitted work at the study is not mechababs', and a run recorded on top of
    it would not describe the tree it ran in — so the tick refuses before it starts."""

    def dirty(root, *, what="this operation"):
        raise RuntimeError(f"{root} is not clean — refusing {what}.")

    monkeypatch.setattr(iterate_mod, "require_clean_shallow", dirty)
    with pytest.raises(RuntimeError, match="not clean"):
        iterate_mod.tick(study, LABEL)
    assert tick == [], "the tick dispatched despite a dirty study"


def test_the_tick_never_assumes_it_is_standing_in_the_study(
    study, tick, tmp_path, monkeypatch
):
    """The both-levels lens: at a superstudy the reconciler stands at the super and
    drives member studies, so `study` is a parameter and the cwd is nobody's business.
    """
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    iterate_mod.tick(study, LABEL, batch=1)

    (call,) = dispatched(tick)
    assert Path(call["study"]) == study


def test_a_superstudy_shard_is_refused_with_its_own_message(tmp_path, tick):
    """A campaign dir with config and no statefile is the superstudy shape; a tick
    pointed at it is at the wrong level, which is not the same mistake as a missing
    campaign."""
    super_root = tmp_path / "superstudy"
    campaign_mod.campaign_dir(super_root, LABEL).mkdir(parents=True)
    campaign_mod.config_path(super_root, LABEL).write_text("label: e2e\n")

    with pytest.raises(SystemExit, match="Per-cell state lives in a study"):
        iterate_mod.tick(super_root, LABEL)


# --------------------------------------------------------------------------
# --dry-run
# --------------------------------------------------------------------------


def test_dry_run_routes_for_real_and_dispatches_nothing(study, tick):
    """The routing is read-only, so it runs; the dispatches are told not to act."""
    write(study, [cell(ANCHOR, babs=ANCHOR_PROJECT)])
    tick.status = dict(ALL_DONE)
    before = campaign_mod.state_path(study, LABEL).read_text()

    iterate_mod.tick(study, LABEL, dry_run=True)

    assert verbs(tick) == ["babs status", "merge"], "the live query was skipped"
    assert dispatched(tick)[0]["dry_run"] is True
    assert campaign_mod.state_path(study, LABEL).read_text() == before


def test_dry_run_says_it_shows_only_this_ticks_transitions(study, tick, capsys):
    iterate_mod.tick(study, LABEL, dry_run=True)
    assert "DRY-RUN" in capsys.readouterr().err
