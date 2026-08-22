"""Read and decide from ``babs status --json`` — the reconciler's decision seam.

Small and pure so the decision is unit-testable and the one spot that knows the
``babs status --json`` shape is isolated. We use only
``total``/``submitted``/``done``/``failed`` and ignore the live-state buckets
(pending/running/completing/configuring): those are a raw scheduler snapshot that
can transiently overlap ``done``, so in-progress is derived as
``submitted - done - failed`` rather than summed.
"""

import json
import subprocess

from mechababs import campaign as campaign_mod


def read_status(project):
    """Run ``babs status --json <project>`` and ``json.loads`` its stdout (a dict).

    The pinned babs (``campaign.babs_bin``), never PATH's: this is the query the
    reconciler routes on and the merge verb's self-guard re-checks, so asking a
    different babs than the one that ran the jobs would be worse than not asking.
    """
    out = subprocess.run(
        [campaign_mod.babs_bin(), "status", "--json", str(project)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


def decide(status):
    """Next transition from the ``--json`` counts, checked in order: unsubmitted ->
    "submit"; in-progress (submitted-done-failed) -> "skip"; failed -> "fail"
    (don't merge a failed set); else -> "merge"."""
    unsubmitted = status["total"] - status["submitted"]
    in_progress = status["submitted"] - status["done"] - status["failed"]
    if unsubmitted > 0:
        return "submit"
    if in_progress > 0:
        return "skip"
    if status["failed"] > 0:
        return "fail"
    return "merge"
