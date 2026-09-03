# mechababs — interventions

The reconciler ([overview.md](overview.md)) advances a cell that is going well, and leaves one alone when it is not.
It does not paper over a problem or retry its way past it.
Recovery is a human act, and mechababs' job is to make that act provenance-safe: the intervention is recorded, not smoothed away.
Messy science is unavoidable, so the study captures the mess honestly instead of pretending the run was clean.

Three things you do when a cell is not going well: find out what failed, repair the derivative in place, or change the code or config and redo the cell.

## Finding the failure

When a cell's jobs end without results, `iterate` says so and does not merge:

```
!! sourcedata/ds000001 / MRIQC-24.0.2: 3 job(s) FAILED — NOT merging.
```

Nothing is written; the flag is this iterate's reading of babs's live counts, and the next iterate reads them again.
`mechababs status` shows the cell as `FAILED`, and `mechababs jobs --failed` lists each failed job with its subject, SLURM id, and the directory its logs are in.
Read the log, decide what happened, then pick one of the two repairs below.

## Per-job surgery: repair a derivative in place

When a job failed for a reason a human has to fix, such as an out-of-memory kill needing more memory, the derivative is repaired rather than redone.

1. Edit the job script inside the derivative: in `derivatives/<name>/code/participant_job.sh`, `#SBATCH --mem=24G` becomes `40G`.
2. From the study root, `datalad save -r -d . derivatives/<name>`.
   Path-scoped, so the change lands as one commit in the derivative and one in the study, and clean sibling cells are untouched.
3. `babs submit derivatives/<name>` resubmits only the jobs without results, leaving the successful ones alone.
4. `mechababs iterate` re-derives the cell's state on the next iterate; the earlier flag was a per-iterate reading, not a persisted state, so once the jobs succeed the cell merges.

This works for the SBATCH-level settings babs reads from the job script when it submits.
Changing the app's own invocation is a different kind of change and is not covered here.

**Provenance consequence:** the derivative is deliberately heterogeneous, some subjects at the old setting and some at the new, and the recorded config no longer reproduces every job.
That is what makes the intervention worth recording, and why the edit is a commit in the derivative's own history rather than a hand fix.

## Changing the code or config, and redoing a cell

The campaign's environment and configs are mutable through git history: edit, commit, and the change reaches every cell scaffolded from then on.
A cell that is already scaffolded keeps what babs baked into it at init, so a change does not reach it until it is redone.

**To bump mechababs or babs**, edit the pin in `.mechababs/campaigns/<label>/pyproject.toml` (the `rev` lines under `[tool.uv.sources]`) and run `mechababs campaign update-env`.
It re-resolves the lock, installs it into the campaign venv, and commits both; the lock's history is the record of which code ran when.
At a superstudy, follow it with `campaign update-env --study <member>` for each member study whose remaining work should move onto the new environment.

**To change an app or cluster config**, edit the copy in `.mechababs/campaigns/<label>/` and commit it.

**To redo a cell** under the new code or config, retire its derivative and let the next iterate re-scaffold it:

```bash
mechababs retire-derivative derivatives/<name> --path /scratch/retired   # keep the evidence
mechababs retire-derivative derivatives/<name> --remove                  # or discard it
mechababs iterate
```

`--path` archives the derivative, with its logs, history and run records, at a directory that must be outside the study; `--remove` deletes it.
Either way the cell is reset in the same transition, and its next scaffold uses whatever the campaign now declares.
The details of both flags are in the [reference](reference.md#retire-derivative).

So a mechababs bump takes effect on the next iterate for cells not yet scaffolded, while reaching a scaffolded cell always means retire and re-scaffold.
That asymmetry is not visible from the layout, and it is the step most often missed.
