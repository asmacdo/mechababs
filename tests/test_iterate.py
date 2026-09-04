"""The reconciler: the routing table, the gate, the batch, and the flock.

Everything `iterate` *dispatches* is stubbed — a real transition needs a real babs, a
real scheduler and a real container, which is the e2e's job. What is mechababs' here
is the decision: given a shard and (for an active cell) a set of live counts, which
verb does each cell get, and which cells get nothing at all.

The dispatch stubs mutate the shard the way the real verbs do (scaffold records
`babs`, merge sets `merged`), because `iterate` re-reads it between cells — so a
stub that only recorded the call would make the multi-cell cases lie.
"""

import sys
from pathlib import Path

import pytest

from mechababs import campaign as campaign_mod
from mechababs import iterate as iterate_mod
from mechababs import scaffold as scaffold_mod

LABEL = "e2e"
ANCHOR = "bids-app-configs/SimBIDS-0.0.3+anchor.yaml"
CHAIN = "bids-app-configs/SimBIDS-0.0.3+chain.yaml"
SOURCEDATA = "sourcedata/ds999999"
ANCHOR_PROJECT = "derivatives/SimBIDS-0.0.3+anchor+ds999999+e2e"

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


def select(monkeypatch, root):
    """Stand `run_iterate` at `root` with the campaign selected, venv check and all.

    `require_selected_campaign` is the configured-level check plus the env guard;
    what it resolves is stubbed so the tests here stay about the reconciler.
    """
    monkeypatch.setattr(
        campaign_mod,
        "require_selected_campaign",
        lambda path=".", **kw: campaign_mod.Selected(
            root, LABEL, campaign_mod.campaign_dir(root, LABEL), root
        ),
    )


@pytest.fixture
def study(tmp_path, monkeypatch):
    """A study with a two-cell shard: an anchor, and a chain that depends on it.

    Deliberately NOT the cwd — see `test_iterate_never_assumes_it_is_standing_in_the_study`.
    """
    study = tmp_path / "study-ds999999"
    campaign_mod.campaign_dir(study, LABEL).mkdir(parents=True)
    campaign_mod.state_path(study, LABEL).write_text(campaign_mod.initial_header())
    write(study, [cell(ANCHOR), cell(CHAIN, depends_on=ANCHOR)])
    select(monkeypatch, study)
    return study


class _DispatchLog(list):
    """The ticks, in dispatch order — plus the babs queries — and the knobs the fakes read.

    `status` is what `babs status` reports for an active cell (one dict for every
    cell, which is enough: no test here needs two cells active at once with
    *different* counts). `cleans` counts the once-per-iterate clean check; `locks`
    counts the flock.
    """

    status = None
    cleans = 0
    locks = 0

    def __init__(self, *a):
        super().__init__(*a)
        self.lock_paths = []


@pytest.fixture
def dispatch_log(monkeypatch):
    """Stub the three dispatches, the babs query, the clean check and the flock."""
    calls = _DispatchLog()
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
                # The real verbs write the shard themselves, and iterate re-reads it
                # between cells — so the stub has to move the state too.
                rows = campaign_mod.read_state(study_arg, label)
                row = campaign_mod.find_cell(rows, source_dataset, app_config)
                row[column] = value or scaffold_mod.derivative_path(
                    source_dataset, app_config, label
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

    def fake_clean(root, *, what="this operation", ignore=()):
        calls.cleans += 1

    monkeypatch.setattr(iterate_mod, "require_clean_shallow", fake_clean)

    real_flocked = iterate_mod.utils.flocked

    def counting_flock(lock):
        calls.locks += 1
        calls.lock_paths.append(Path(lock))
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
# One cell: what each state gets dispatched
# --------------------------------------------------------------------------


def test_a_not_started_cell_is_scaffolded(study, dispatch_log):
    iterate_mod.run_iterate(str(study), batch=1)

    (call,) = dispatched(dispatch_log)
    assert call["verb"] == "scaffold"
    assert call["cell"] == (SOURCEDATA, ANCHOR)
    assert call["study"] == str(study)
    assert call["label"] == LABEL


def test_a_waiting_cell_is_noted_and_passed_over_not_blocked_on(
    study, dispatch_log, capsys
):
    """Gating is noting: iterate says so and moves on, and the next one re-checks."""
    iterate_mod.run_iterate(str(study))

    assert [c["cell"] for c in dispatched(dispatch_log)] == [(SOURCEDATA, ANCHOR)], (
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
    study, dispatch_log, status, verb
):
    write(study, [cell(ANCHOR, babs=ANCHOR_PROJECT)])
    dispatch_log.status = dict(status)

    iterate_mod.run_iterate(str(study))

    assert verbs(dispatch_log) == ["babs status", verb]
    assert dispatch_log[0]["project"] == str(study / ANCHOR_PROJECT)


def test_jobs_still_in_flight_are_left_alone(study, dispatch_log):
    write(study, [cell(ANCHOR, babs=ANCHOR_PROJECT)])
    dispatch_log.status = dict(STILL_RUNNING)

    assert iterate_mod.run_iterate(str(study)) == []
    assert dispatched(dispatch_log) == [], "a running cell was advanced"


def test_a_failed_cell_is_flagged_loudly_and_never_merged(study, dispatch_log, capsys):
    """Merging a partial set is silent, not loud — babs merges whatever branches it
    finds — so the reconciler refuses and says so unmistakably."""
    write(study, [cell(ANCHOR, babs=ANCHOR_PROJECT), cell(CHAIN)])
    dispatch_log.status = dict(SOME_FAILED)

    iterate_mod.run_iterate(str(study))

    assert "merge" not in verbs(dispatch_log), "a failed cell was merged"
    err = capsys.readouterr().err
    assert "FAILED" in err and "NOT merging" in err, err
    assert f"babs status {ANCHOR_PROJECT}" in err, "the flag does not say where to look"


def test_a_failure_does_not_halt_the_iterate(study, dispatch_log):
    """Level-triggered: one stuck cell must not stop the campaign's other cells."""
    write(study, [cell(ANCHOR, babs=ANCHOR_PROJECT), cell(CHAIN)])
    dispatch_log.status = dict(SOME_FAILED)

    iterate_mod.run_iterate(str(study))

    assert [c["cell"] for c in dispatched(dispatch_log)] == [(SOURCEDATA, CHAIN)], (
        "the cells after the failed one never got their turn"
    )


def test_a_failure_is_a_per_iterate_reading_not_a_column(study, dispatch_log):
    """Nothing is written, so a repair-and-resubmit takes effect with nothing to clear:
    the same shard routes to `merge` the moment the counts change."""
    write(study, [cell(ANCHOR, babs=ANCHOR_PROJECT)])
    dispatch_log.status = dict(SOME_FAILED)
    before = campaign_mod.state_path(study, LABEL).read_text()

    iterate_mod.run_iterate(str(study))
    assert campaign_mod.state_path(study, LABEL).read_text() == before

    dispatch_log.status = dict(ALL_DONE)
    iterate_mod.run_iterate(str(study))
    assert "merge" in verbs(dispatch_log)


def test_a_merged_cell_is_skipped_without_asking_babs(study, dispatch_log):
    """The economy the `merged` column buys: a done cell costs no query at all."""
    write(study, [cell(ANCHOR, babs=ANCHOR_PROJECT, merged="true")])

    assert iterate_mod.run_iterate(str(study)) == []
    assert dispatch_log == [], "a merged cell was queried or advanced"


def test_each_cell_advances_by_at_most_one_transition(study, dispatch_log):
    """A not-started cell scaffolds and stops — it does not go on to submit."""
    write(study, [cell(ANCHOR)])
    dispatch_log.status = dict(UNSUBMITTED)

    iterate_mod.run_iterate(str(study))

    assert verbs(dispatch_log) == ["scaffold"]


def test_a_producer_that_merges_mid_iterate_opens_its_dependants_gate(
    study, dispatch_log
):
    """The shard is re-read per cell, so ground truth is what routes each one —
    including a change an earlier tick in this same iterate made."""
    write(study, [cell(ANCHOR, babs=ANCHOR_PROJECT), cell(CHAIN, depends_on=ANCHOR)])
    dispatch_log.status = dict(ALL_DONE)

    iterate_mod.run_iterate(str(study))

    assert [(c["verb"], c["cell"][1]) for c in dispatched(dispatch_log)] == [
        ("merge", ANCHOR),
        ("scaffold", CHAIN),
    ]


# --------------------------------------------------------------------------
# Scope: --batch and --app
# --------------------------------------------------------------------------


def test_batch_bounds_the_cells_that_advance(study, dispatch_log):
    write(study, [cell(ANCHOR), cell(CHAIN)])

    assert len(iterate_mod.run_iterate(str(study), batch=1)) == 1
    assert [c["cell"][1] for c in dispatched(dispatch_log)] == [ANCHOR]


def test_a_cell_that_does_not_advance_does_not_consume_batch(study, dispatch_log):
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

    assert len(iterate_mod.run_iterate(str(study), batch=1)) == 1
    assert [c["cell"][1] for c in dispatched(dispatch_log)] == [
        "bids-app-configs/Third.yaml"
    ]


def test_derivative_narrows_to_one_apps_cells(study, dispatch_log):
    write(study, [cell(ANCHOR), cell(CHAIN)])

    iterate_mod.run_iterate(str(study), app="SimBIDS-0.0.3+chain")

    assert [c["cell"][1] for c in dispatched(dispatch_log)] == [CHAIN]


def test_a_derivative_that_matches_nothing_is_a_typo_not_an_empty_iterate(
    study, dispatch_log
):
    with pytest.raises(SystemExit, match="no cells for --app"):
        iterate_mod.run_iterate(str(study), app="MRIQC-24.0.2")
    assert dispatch_log == []


# --------------------------------------------------------------------------
# iterate's own guarantees
# --------------------------------------------------------------------------


def test_the_flock_is_taken_exactly_once_around_the_whole_iterate(
    study, dispatch_log, monkeypatch
):
    """One lock, held across every cell: the level is the single-writer unit. It
    must not be taken per cell (and never inside a verb this iterate dispatches — an
    flock is per open-file-description, so that would deadlock against this one).

    Driven through `run_iterate`, which is where the lock is taken, around the
    whole fan-out: a lock per member would be released between them.
    """
    write(study, [cell(ANCHOR), cell(CHAIN)])

    iterate_mod.run_iterate(str(study))

    assert dispatch_log.locks == 1, f"the flock was taken {dispatch_log.locks} times"
    assert dispatch_log.lock_paths == [campaign_mod.flock_path(study)]
    assert campaign_mod.flock_path(study).exists()


def test_the_clean_check_runs_once_per_iterate(study, dispatch_log):
    write(study, [cell(ANCHOR), cell(CHAIN)])

    iterate_mod.run_iterate(str(study))

    assert dispatch_log.cleans == 1, f"the clean check ran {dispatch_log.cleans} times"


def test_the_clean_check_runs_before_anything_is_dispatched(
    study, monkeypatch, dispatch_log
):
    """Uncommitted work at the study is not mechababs', and a run recorded on top of
    it would not describe the tree it ran in — so iterate refuses before it starts."""

    def dirty(root, *, what="this operation"):
        raise RuntimeError(f"{root} is not clean — refusing {what}.")

    monkeypatch.setattr(iterate_mod, "require_clean_shallow", dirty)
    with pytest.raises(RuntimeError, match="not clean"):
        iterate_mod.run_iterate(str(study))
    assert dispatch_log == [], "iterate dispatched despite a dirty study"


def test_iterate_never_assumes_it_is_standing_in_the_study(
    study, dispatch_log, tmp_path, monkeypatch
):
    """The both-levels lens: at a superstudy the reconciler stands at the super and
    drives member studies, so `study` is a parameter and the cwd is nobody's business.
    """
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    iterate_mod.run_iterate(str(study), batch=1)

    (call,) = dispatched(dispatch_log)
    assert Path(call["study"]) == study


def test_a_superstudy_shard_is_refused_with_its_own_message(
    tmp_path, dispatch_log, monkeypatch
):
    """A campaign dir with config and no statefile is the superstudy shape; an iterate
    pointed at it is at the wrong level, which is not the same mistake as a missing
    campaign."""
    super_root = tmp_path / "superstudy"
    campaign_mod.campaign_dir(super_root, LABEL).mkdir(parents=True)
    campaign_mod.config_path(super_root, LABEL).write_text("label: e2e\n")
    select(monkeypatch, super_root)

    with pytest.raises(SystemExit, match="Per-cell state lives in a study"):
        iterate_mod.run_iterate(str(super_root))


# --------------------------------------------------------------------------
# --dry-run
# --------------------------------------------------------------------------


def test_dry_run_routes_for_real_and_dispatches_nothing(study, dispatch_log):
    """The routing is read-only, so it runs; the dispatches are told not to act."""
    write(study, [cell(ANCHOR, babs=ANCHOR_PROJECT)])
    dispatch_log.status = dict(ALL_DONE)
    before = campaign_mod.state_path(study, LABEL).read_text()

    iterate_mod.run_iterate(str(study), dry_run=True)

    assert verbs(dispatch_log) == ["babs status", "merge"], "the live query was skipped"
    assert dispatched(dispatch_log)[0]["dry_run"] is True
    assert campaign_mod.state_path(study, LABEL).read_text() == before


def test_dry_run_says_it_shows_only_this_iterates_transitions(
    study, dispatch_log, capsys
):
    iterate_mod.run_iterate(str(study), dry_run=True)
    assert "DRY-RUN" in capsys.readouterr().err


# --- at a superstudy: fanning out to members --------------------------------
#
# The loop was always parameterized on the study, so the level adds a fan-out rather
# than a second reconciler. What these pin is the fan-out's shape: catalog order,
# per-member batching, and the narrowing that does NOT change the level.


@pytest.fixture
def superstudy(tmp_path, monkeypatch):
    """A superstudy whose campaign covers two members, each with a one-cell shard."""
    root = tmp_path / "my-super"
    campaign_mod.campaign_dir(root, LABEL).mkdir(parents=True)
    members = []
    for name in ("study-dsA", "study-dsB"):
        member = root / name
        campaign_mod.campaign_dir(member, LABEL).mkdir(parents=True)
        # Installed — iterate advances only members that are actually here, so a
        # fixture member has to look present the way `study.is_study_root` reads it.
        (member / ".datalad").mkdir()
        campaign_mod.state_path(member, LABEL).write_text(campaign_mod.initial_header())
        write(member, [cell(ANCHOR)])
        members.append(member)
    campaign_mod.write_members(
        root,
        LABEL,
        [
            {
                "study": "study-dsA",
                "source_dataset": SOURCEDATA,
                "lifecycle": campaign_mod.LIFECYCLE_REGISTERED,
            },
            {
                "study": "study-dsB",
                "source_dataset": SOURCEDATA,
                "lifecycle": campaign_mod.LIFECYCLE_REGISTERED,
            },
        ],
    )

    monkeypatch.setattr(
        campaign_mod,
        "require_selected_campaign",
        lambda path=".", **kw: campaign_mod.Selected(
            root, LABEL, campaign_mod.campaign_dir(root, LABEL), root
        ),
    )
    # The fixture tree is plain directories, so the super's own datalad calls are
    # recorded rather than run. What they record is the point of the tests below.
    recorded = []
    monkeypatch.setattr(
        iterate_mod.utils,
        "save_paths",
        lambda root_, paths, message: recorded.append((root_, list(paths), message)),
    )
    monkeypatch.setattr(
        iterate_mod.utils, "require_clean_gitlink", lambda root_, member: None
    )
    return root, members, recorded


def test_a_superstudy_iterate_advances_every_member(superstudy, dispatch_log):
    root, members, _saves = superstudy

    iterate_mod.run_iterate(str(root))

    assert [call["study"] for call in dispatch_log] == [str(m) for m in members]


def test_members_advance_in_catalog_order(superstudy, dispatch_log):
    """Catalog order is the ordering interface at the super, the way row order is
    within a shard."""
    root, members, _saves = superstudy
    campaign_mod.write_members(
        root,
        LABEL,
        [
            {
                "study": "study-dsB",
                "source_dataset": SOURCEDATA,
                "lifecycle": campaign_mod.LIFECYCLE_REGISTERED,
            },
            {
                "study": "study-dsA",
                "source_dataset": SOURCEDATA,
                "lifecycle": campaign_mod.LIFECYCLE_REGISTERED,
            },
        ],
    )

    iterate_mod.run_iterate(str(root))

    assert [Path(call["study"]).name for call in dispatch_log] == [
        "study-dsB",
        "study-dsA",
    ]


def test_a_member_selected_twice_is_advanced_once(superstudy, dispatch_log):
    """Several source datasets in one member give several catalog rows, one member."""
    root, _, _saves = superstudy
    campaign_mod.write_members(
        root,
        LABEL,
        [
            {
                "study": "study-dsA",
                "source_dataset": SOURCEDATA,
                "lifecycle": campaign_mod.LIFECYCLE_REGISTERED,
            },
            {
                "study": "study-dsA",
                "source_dataset": "sourcedata/other",
                "lifecycle": campaign_mod.LIFECYCLE_REGISTERED,
            },
        ],
    )

    iterate_mod.run_iterate(str(root))

    assert [Path(call["study"]).name for call in dispatch_log] == ["study-dsA"]


def test_study_narrows_to_one_member(superstudy, dispatch_log):
    root, _, _saves = superstudy

    iterate_mod.run_iterate(str(root), study="study-dsB")

    assert [Path(call["study"]).name for call in dispatch_log] == ["study-dsB"]


def test_a_study_that_is_not_a_member_is_a_typo_not_an_empty_iterate(
    superstudy, dispatch_log
):
    """Matched against the catalog, not the filesystem: a directory that exists but
    was never selected into this campaign is an error, not a silent no-op."""
    root, _, _saves = superstudy
    (root / "study-dsC").mkdir()

    with pytest.raises(SystemExit) as excinfo:
        iterate_mod.run_iterate(str(root), study="study-dsC")
    assert "not a member" in str(excinfo.value)


def test_batch_bounds_the_whole_iterate_not_each_member(superstudy, dispatch_log):
    """`--batch N` means one thing at either level: at most N cells advance. At a
    superstudy the budget is spent in catalog order, which is what makes the catalog
    a priority interface — it decides who gets the budget, not only who goes first."""
    root, members, _saves = superstudy

    iterate_mod.run_iterate(str(root), batch=1)

    assert [call["study"] for call in dispatch_log] == [str(members[0])]


def test_a_spent_batch_stops_the_fan_out_before_the_next_member_is_touched(
    superstudy, dispatch_log, monkeypatch
):
    """An iterate with nothing left to spend must not touch the filesystem to find out."""
    root, _members, _saves = superstudy
    checked = []
    monkeypatch.setattr(
        iterate_mod.utils,
        "require_clean_gitlink",
        lambda root_, member: checked.append(member),
    )

    iterate_mod.run_iterate(str(root), batch=1)

    assert checked == ["study-dsA"]


def test_an_unspent_batch_carries_on_to_the_next_member(superstudy, dispatch_log):
    """The budget is the iterate's, so what one member does not spend the next can."""
    root, members, _saves = superstudy

    iterate_mod.run_iterate(str(root), batch=2)

    assert [call["study"] for call in dispatch_log] == [str(m) for m in members]


def test_study_is_refused_for_a_study_configured_campaign(study, dispatch_log):
    with pytest.raises(SystemExit) as excinfo:
        iterate_mod.run_iterate(str(study), study="study-dsA")
    assert "no members to select between" in str(excinfo.value)


def test_one_lock_at_the_super_covers_the_whole_fan_out(superstudy, dispatch_log):
    """The single writer is the campaign, and the campaign is operated at the super.

    A per-member lock would be released between members — leaving the gaps it exists
    to close — and would cover none of the super's own writes (the gitlink and the
    catalog row), which are precisely what a second concurrent iterate would collide
    with.
    """
    root, members, _ = superstudy

    iterate_mod.run_iterate(str(root))

    assert dispatch_log.locks == 1, (
        f"the flock was taken {dispatch_log.locks} times for 2 members"
    )
    assert dispatch_log.lock_paths == [campaign_mod.flock_path(root)]
    for member in members:
        assert not campaign_mod.flock_path(member).exists(), (
            f"{member.name} was locked as if it were its own single-writer unit"
        )


def test_each_member_is_checked_before_it_is_touched_and_scoped_to_itself(
    superstudy, dispatch_log, monkeypatch
):
    """The member's own tree is covered at its own level (the shallow check, then the
    transition's `datalad run`), so what is left for the super is whether its gitlink
    still matches the member's HEAD. Scoped to the one member, the check costs the
    same in a superstudy of a thousand as in one of two."""
    root, members, _saves = superstudy
    order = []

    monkeypatch.setattr(
        iterate_mod.utils,
        "require_clean_gitlink",
        lambda root_, member: order.append(("gitlink", member)),
    )
    monkeypatch.setattr(
        iterate_mod,
        "require_clean_shallow",
        lambda root_, **kw: order.append(("shallow", Path(root_).name)),
    )

    iterate_mod.run_iterate(str(root))

    assert order == [
        ("shallow", "my-super"),
        ("gitlink", "study-dsA"),
        ("shallow", "study-dsA"),
        ("gitlink", "study-dsB"),
        ("shallow", "study-dsB"),
    ]


def test_the_super_is_checked_once_before_any_member_is_touched(
    superstudy, dispatch_log, monkeypatch
):
    """The per-member check is scoped to a member, so it cannot see dirt in the
    super's own tree. That is what this one is for, and once per iterate is enough."""
    root, _members, _saves = superstudy
    order = []

    monkeypatch.setattr(
        iterate_mod,
        "require_clean_shallow",
        lambda root_, **kw: order.append(("shallow", Path(root_).name)),
    )
    monkeypatch.setattr(
        iterate_mod.utils,
        "require_clean_gitlink",
        lambda root_, member: order.append(("gitlink", member)),
    )

    iterate_mod.run_iterate(str(root))

    assert order[0] == ("shallow", "my-super")
    assert order.count(("shallow", "my-super")) == 1


def test_an_uninstalled_member_is_left_alone(superstudy, dispatch_log):
    """A member the user pushed and uninstalled is skipped, never reinstalled to
    advance it: reclaiming space is a decision an iterate must not quietly reverse."""
    root, members, _saves = superstudy
    (members[0] / ".datalad").rmdir()

    iterate_mod.run_iterate(str(root))

    assert [call["study"] for call in dispatch_log] == [str(members[1])]


def test_an_uninstalled_member_is_left_alone_even_when_named(superstudy, dispatch_log):
    """Naming it with --study does not override this — it is still not here."""
    root, members, _saves = superstudy
    (members[0] / ".datalad").rmdir()

    iterate_mod.run_iterate(str(root), study="study-dsA")

    assert dispatch_log == []


def test_an_uninstalled_member_is_not_recorded_at_the_super(superstudy, dispatch_log):
    """Nothing advanced, so there is no gitlink to register."""
    root, members, saves = superstudy
    (members[0] / ".datalad").rmdir()

    iterate_mod.run_iterate(str(root), study="study-dsA")

    assert saves == []


def test_each_member_is_recorded_at_the_super_as_it_advances(superstudy, dispatch_log):
    """A study-only campaign needs none of this — the transition's own `datalad run`
    commits in the study, which is the operating level. With a super above, that run
    leaves the member's gitlink advanced and only the super can register it."""
    root, members, saves = superstudy

    iterate_mod.run_iterate(str(root))

    assert [[Path(p).name for p in paths] for _root, paths, _msg in saves] == [
        [member.name, campaign_mod.MEMBERS_FILENAME] for member in members
    ]
    assert all(saved_root == root for saved_root, _, _ in saves)
    assert all(LABEL in message for _, _, message in saves)


def test_a_member_that_does_not_advance_is_not_recorded(superstudy, dispatch_log):
    """Nothing moved, nothing to register — an empty commit at the super would say
    a member advanced when it did not."""
    root, _members, saves = superstudy
    for member in _members:
        rows = campaign_mod.read_state(member, LABEL)
        rows[0]["merged"] = "yes"
        campaign_mod.write_state(member, LABEL, rows)

    iterate_mod.run_iterate(str(root))

    assert saves == []


def test_a_dry_run_records_nothing_at_the_super(superstudy, dispatch_log):
    root, _members, saves = superstudy

    iterate_mod.run_iterate(str(root), dry_run=True)

    assert saves == []


# --- the lifecycle a superstudy commits ------------------------------------------


def test_a_source_dataset_with_nothing_started_is_registered():
    assert (
        iterate_mod.source_lifecycle([cell(ANCHOR), cell(CHAIN)], SOURCEDATA)
        == campaign_mod.LIFECYCLE_REGISTERED
    )


def test_a_source_dataset_is_active_once_any_cell_has_a_babs_project():
    rows = [cell(ANCHOR, babs=ANCHOR_PROJECT), cell(CHAIN)]

    assert (
        iterate_mod.source_lifecycle(rows, SOURCEDATA) == campaign_mod.LIFECYCLE_ACTIVE
    )


def test_a_source_dataset_still_reads_active_when_only_some_cells_merged():
    """The whole point of the coarse value: `merged` must mean all of them."""
    rows = [cell(ANCHOR, babs=ANCHOR_PROJECT, merged="true"), cell(CHAIN)]

    assert (
        iterate_mod.source_lifecycle(rows, SOURCEDATA) == campaign_mod.LIFECYCLE_ACTIVE
    )


def test_a_source_dataset_is_merged_only_when_every_one_of_its_cells_is():
    rows = [
        cell(ANCHOR, babs=ANCHOR_PROJECT, merged="true"),
        cell(CHAIN, babs="derivatives/chain", merged="true"),
    ]

    assert (
        iterate_mod.source_lifecycle(rows, SOURCEDATA) == campaign_mod.LIFECYCLE_MERGED
    )


def test_a_lifecycle_reads_only_its_own_source_datasets_cells():
    """A study may hold several source datasets, each with its own catalog row — so a
    busy neighbour must not make a finished one read active."""
    other = dict(cell(ANCHOR), source_dataset="sourcedata/ds111111")
    rows = [cell(ANCHOR, babs=ANCHOR_PROJECT, merged="true"), other]

    assert (
        iterate_mod.source_lifecycle(rows, SOURCEDATA) == campaign_mod.LIFECYCLE_MERGED
    )


def lifecycles(root):
    return {
        row["study"]: row["lifecycle"] for row in campaign_mod.read_members(root, LABEL)
    }


def test_scaffolding_a_members_first_cell_moves_it_to_active(superstudy, dispatch_log):
    root, _members, _saves = superstudy

    iterate_mod.run_iterate(str(root), study="study-dsA")

    assert lifecycles(root)["study-dsA"] == campaign_mod.LIFECYCLE_ACTIVE
    assert lifecycles(root)["study-dsB"] == campaign_mod.LIFECYCLE_REGISTERED, (
        "a member this iterate never touched had its lifecycle rewritten"
    )


def test_merging_a_members_last_cell_moves_it_to_merged(superstudy, dispatch_log):
    root, members, saves = superstudy
    write(members[0], [cell(ANCHOR, babs=ANCHOR_PROJECT)])
    dispatch_log.status = dict(ALL_DONE)

    iterate_mod.run_iterate(str(root), study="study-dsA")

    assert lifecycles(root)["study-dsA"] == campaign_mod.LIFECYCLE_MERGED
    (_root, _paths, message) = saves[0]
    assert message.startswith(
        f"mechababs iterate: study-dsA {SOURCEDATA} is now merged"
    ), message


def set_lifecycle(root, study, value):
    rows = campaign_mod.read_members(root, LABEL)
    for row in rows:
        if row["study"] == study:
            row["lifecycle"] = value
    campaign_mod.write_members(root, LABEL, rows)


def test_the_catalog_is_written_only_when_the_lifecycle_actually_changed(
    superstudy, dispatch_log
):
    """A second scaffold in an already-active member moves the gitlink without
    moving the coarse value, so the save carries the gitlink alone — the catalog is
    not rewritten to the same thing."""
    root, members, saves = superstudy
    write(members[0], [cell(ANCHOR, babs=ANCHOR_PROJECT), cell(CHAIN)])
    set_lifecycle(root, "study-dsA", campaign_mod.LIFECYCLE_ACTIVE)
    dispatch_log.status = dict(STILL_RUNNING)

    iterate_mod.run_iterate(str(root), study="study-dsA")

    ((_root, paths, message),) = saves
    assert [Path(p).name for p in paths] == ["study-dsA"]
    assert f"scaffolded {SOURCEDATA} / SimBIDS-0.0.3+chain" in message, message


def test_a_submit_is_not_recorded_at_the_super(superstudy, dispatch_log):
    """Submit hands jobs to the scheduler and writes nothing the study tracks, so
    the member's gitlink does not move and there is nothing for the super to
    commit. A save here would only ever come back `notneeded`."""
    root, members, saves = superstudy
    write(members[0], [cell(ANCHOR, babs=ANCHOR_PROJECT)])
    set_lifecycle(root, "study-dsA", campaign_mod.LIFECYCLE_ACTIVE)
    dispatch_log.status = dict(UNSUBMITTED)

    iterate_mod.run_iterate(str(root), study="study-dsA")

    assert verbs(dispatch_log) == ["babs status", "submit"]
    assert saves == []


def test_each_cell_transition_is_its_own_save_and_names_its_cell(
    superstudy, dispatch_log
):
    """One cell-transition, one commit at the super: two derivatives in one member
    are two subdatasets whose provenance has nothing to do with each other, so a
    save spanning both would mix them. With no lifecycle change to lead with, the
    subject is the transition, naming the cell rather than counting cells."""
    root, members, saves = superstudy
    write(members[0], [cell(ANCHOR), cell(CHAIN)])
    set_lifecycle(root, "study-dsA", campaign_mod.LIFECYCLE_ACTIVE)

    iterate_mod.run_iterate(str(root), study="study-dsA")

    subjects = [message.splitlines()[0] for _root, _paths, message in saves]
    assert subjects == [
        f"mechababs iterate: study-dsA scaffolded {SOURCEDATA} / SimBIDS-0.0.3+anchor "
        f"(campaign {LABEL!r})",
        f"mechababs iterate: study-dsA scaffolded {SOURCEDATA} / SimBIDS-0.0.3+chain "
        f"(campaign {LABEL!r})",
    ]
    assert all("cell" not in message for _, _, message in saves), (
        "the count message survived"
    )


def test_a_merge_that_is_not_the_last_holds_the_lifecycle_at_active(
    superstudy, dispatch_log
):
    """Two merges in one member are two saves. The first leaves a sibling cell
    unmerged, so its subject is the transition; the second is the last, so its
    subject is the lifecycle move."""
    root, members, saves = superstudy
    chain_project = scaffold_mod.derivative_path(SOURCEDATA, CHAIN, LABEL)
    write(
        members[0],
        [cell(ANCHOR, babs=ANCHOR_PROJECT), cell(CHAIN, babs=chain_project)],
    )
    set_lifecycle(root, "study-dsA", campaign_mod.LIFECYCLE_ACTIVE)
    dispatch_log.status = dict(ALL_DONE)

    iterate_mod.run_iterate(str(root), study="study-dsA")

    subjects = [message.splitlines()[0] for _root, _paths, message in saves]
    assert subjects == [
        f"mechababs iterate: study-dsA merged {SOURCEDATA} / SimBIDS-0.0.3+anchor "
        f"(campaign {LABEL!r})",
        f"mechababs iterate: study-dsA {SOURCEDATA} is now merged (campaign {LABEL!r})",
    ]
    assert [Path(p).name for p in saves[0][1]] == ["study-dsA"]
    assert [Path(p).name for p in saves[1][1]] == [
        "study-dsA",
        campaign_mod.MEMBERS_FILENAME,
    ]


def test_a_failure_mid_member_leaves_the_super_as_far_as_the_last_success(
    superstudy, dispatch_log, monkeypatch
):
    """Each cell-transition is recorded before the next cell is attempted, so a
    dispatch that dies on the second cell leaves the first one registered at the
    super. The next `iterate` then finds the gitlink current rather than refusing
    with a message that blames an intervention nobody made."""
    root, members, saves = superstudy
    write(members[0], [cell(ANCHOR), cell(CHAIN)])
    real_scaffold = iterate_mod.dispatch.scaffold

    def scaffold_then_die(study_arg, label, source_dataset, app_config, **kw):
        if app_config == CHAIN:
            raise RuntimeError("babs init failed")
        return real_scaffold(study_arg, label, source_dataset, app_config, **kw)

    monkeypatch.setattr(iterate_mod.dispatch, "scaffold", scaffold_then_die)

    with pytest.raises(RuntimeError, match="babs init failed"):
        iterate_mod.run_iterate(str(root), study="study-dsA")

    assert len(saves) == 1, saves
    assert f"scaffolded  {SOURCEDATA} / SimBIDS-0.0.3+anchor" in saves[0][2]
    assert lifecycles(root)["study-dsA"] == campaign_mod.LIFECYCLE_ACTIVE


def test_a_failed_inner_command_is_a_message_at_the_cli_not_a_traceback(
    superstudy, dispatch_log, monkeypatch
):
    """The inner command's own output is already on stderr; what the CLI adds is
    that iterate stopped there and that the cells advanced before it stand
    recorded."""
    import subprocess

    from mechababs import cli

    root, members, saves = superstudy
    write(members[0], [cell(ANCHOR), cell(CHAIN)])
    real_scaffold = iterate_mod.dispatch.scaffold

    def scaffold_then_die(study_arg, label, source_dataset, app_config, **kw):
        if app_config == CHAIN:
            raise subprocess.CalledProcessError(1, ["mechababs-inner", "scaffold"])
        return real_scaffold(study_arg, label, source_dataset, app_config, **kw)

    monkeypatch.setattr(iterate_mod.dispatch, "scaffold", scaffold_then_die)
    monkeypatch.setattr(sys, "argv", ["mechababs", "iterate", "--study", "study-dsA"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    message = str(excinfo.value)
    assert "mechababs iterate stopped: `mechababs-inner scaffold` exited 1" in message
    assert "advanced before it is recorded" in message
    assert len(saves) == 1, saves


def test_a_dry_run_writes_no_lifecycle(superstudy, dispatch_log):
    root, _members, _saves = superstudy

    iterate_mod.run_iterate(str(root), dry_run=True)

    assert set(lifecycles(root).values()) == {campaign_mod.LIFECYCLE_REGISTERED}
