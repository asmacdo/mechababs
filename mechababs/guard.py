"""guard.py — preconditions that protect the campaign's provenance integrity.

A campaign records the exact babs + mechababs it ran as pinned subdatasets
(code/babs, code/mechababs). That provenance link is only *causal* — the
derivative was produced by the code the superdataset points at — if the vendored
code that actually runs matches the recorded pin. Uncommitted edits in code/*, or
a checked-out commit that differs from the recorded gitlink, break that: the run
would be attributed to a pin it did not use. So refuse to run when the pins are
dirty.

Scope is deliberately the *pins*, not the whole campaign: an in-progress
derivative left untracked by `iterate` is expected working state, not a
provenance lie. Making the whole campaign clean after each step (so a broader
guard could hold) is the matched postcondition tracked separately (#52).
"""

import subprocess
import sys

PINS = ("code/mechababs", "code/babs")


def require_clean_pins(campaign):
    """Exit if either vendored code pin has uncommitted changes or has drifted
    from the commit the campaign superdataset records.

    `git status --porcelain` at the superdataset level reports a submodule as
    modified for both cases (dirty working tree, or HEAD != the recorded
    gitlink), so one check covers them. A missing pin path yields no output — the
    campaign-shape checks in the CLI are what report "not a campaign".
    """
    result = subprocess.run(
        ["git", "-C", str(campaign), "status", "--porcelain", "--", *PINS],
        check=True,
        text=True,
        capture_output=True,
    )
    dirty = result.stdout.strip()
    if dirty:
        sys.exit(
            "campaign code pins are dirty — refusing to run so provenance stays "
            "causal (the derivative must be produced by the recorded pin):\n"
            f"{dirty}\n"
            "commit or reset code/mechababs + code/babs (and `datalad save` the "
            "campaign to update the recorded pin), then retry."
        )
