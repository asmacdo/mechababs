"""submit.py — the active-cell transition that puts a scaffolded cell's jobs on the queue.

``scaffold`` builds a babs project and stops; nothing in it has run. ``submit``
hands that project to babs, which sbatches the jobs that do not yet have results.
It is the one transition that produces no artifact of its own — what it produces
is jobs, and those live in the scheduler.

**It changes nothing tracked, and that is why it is a plain verb.** babs writes its
job bookkeeping (``code/job_status.csv``, ``code/job_submit.csv``, their locks) into
files its own ``.gitignore`` excludes, fetches container content with
``datalad get``, and commits nothing. So there is no diff to capture and no
``datalad run`` to wrap it in (docs/spec.md: "a submit that only sbatches and
changes nothing tracked needs no `datalad run`"). The dispatcher does not take that
on trust — see ``dispatch.plain``, which fails loudly if a submit ever does move
tracked state.

Submitted-ness is likewise not a column. It is volatile, babs owns it, and the
reconciler asks for it live (``babs status --json``) every iterate rather than keeping
a mirror to drift.
"""

import subprocess
import sys
from pathlib import Path

from mechababs import campaign as campaign_mod


def submit(study, label, source_dataset, app_config):
    """Submit one active cell's outstanding jobs. Returns the cell's ``babs`` path.

    ``babs submit`` deploys every job without results, leaving finished ones alone —
    so this is also the resubmit after a per-job repair, not only the first deploy.
    """
    study = Path(study)
    rows = campaign_mod.read_state(study, label)
    row = campaign_mod.find_cell(rows, source_dataset, app_config)
    project = campaign_mod.require_active_cell(row, "submit")

    # Study-relative, run with the study as cwd: babs absolutizes the project path
    # against cwd, and keeping every path in the verb study-relative is what makes
    # the verb itself indifferent to where the reconciler stands.
    cmd = [campaign_mod.babs_bin(), "submit", project]
    print("+ " + " ".join(cmd), file=sys.stderr)
    subprocess.run(cmd, cwd=str(study), check=True)
    return project
