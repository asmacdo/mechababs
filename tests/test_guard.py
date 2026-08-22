"""Unit tests for the pin-cleanliness guard (guard.require_clean_pins).

The guard runs `git status --porcelain -- code/mechababs code/babs` at the
campaign superdataset level. In prod code/* are submodules; here they are plain
tracked subdirs — the guard command and its empty/non-empty contract are
identical either way, so a subdir campaign faithfully exercises the logic without
the weight of real submodules. (Submodule HEAD-drift is caught by the same
command and covered by the e2e.)
"""

import subprocess

import pytest

from mechababs import guard
from mechababs import state


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def _campaign_with_pins(tmp_path):
    """A minimal campaign git repo with code/mechababs + code/babs committed clean."""
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    _git(campaign, "init", "-q")
    _git(campaign, "config", "user.email", "t@t")
    _git(campaign, "config", "user.name", "t")
    for pin in guard.PINS:
        d = campaign / pin
        d.mkdir(parents=True)
        (d / "marker.py").write_text("x = 1\n")
    _git(campaign, "add", "-A")
    _git(campaign, "commit", "-q", "-m", "vendor pins")
    return campaign


def test_clean_pins_pass(tmp_path):
    campaign = _campaign_with_pins(tmp_path)
    guard.require_clean_pins(campaign)  # no SystemExit


def test_dirty_pin_trips(tmp_path):
    campaign = _campaign_with_pins(tmp_path)
    (campaign / "code" / "babs" / "marker.py").write_text("x = 2  # hand-edited\n")
    with pytest.raises(SystemExit):
        guard.require_clean_pins(campaign)


def test_untracked_file_in_pin_trips(tmp_path):
    campaign = _campaign_with_pins(tmp_path)
    (campaign / "code" / "mechababs" / "sneaky.py").write_text("oops\n")
    with pytest.raises(SystemExit):
        guard.require_clean_pins(campaign)


def test_dirt_outside_pins_ignored(tmp_path):
    """A dirty file outside code/* is not the pin guard's concern — an in-progress
    derivative / unsaved ledger is expected state (making it clean is #52)."""
    campaign = _campaign_with_pins(tmp_path)
    (campaign / state.STATE_FILENAME).write_text("url\n")
    (campaign / "derivatives").mkdir()
    (campaign / "derivatives" / "in_progress.txt").write_text("scaffolding\n")
    guard.require_clean_pins(campaign)  # no SystemExit
