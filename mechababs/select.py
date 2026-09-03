"""select.py — read a study's sourcedata metadata: the add-dataset sniff, and selection.

Two readers of one file, at two different moments. ``add-dataset`` **sniffs** a
source dataset when it is selected into a campaign — how many subjects, how many
sessions, and therefore whether the cell runs subject- or session-level; those are
the statefile's identity columns. Scaffold later **selects** from the same file,
applying a pipeline's eligibility rule to produce that cell's inclusion list.

Both read the study's per-subject metadata TSV
(``sourcedata/sourcedata+subjects[+sessions].tsv``) from the study on disk —
git-tracked there, or ``datalad get`` if annexed. Selection aggregates rows sharing
a ``(sub[,ses])`` key, applies the pipeline's *declarative* eligibility rule (from
its ``selection:`` config), and writes an inclusion CSV for
``babs init --list-sub-file``.

Once the TSV text + rule are in hand, selection is a **pure function** of them: no
network, no app names, no per-pipeline code. A new BIDS app declares its needs in
its pipeline YAML; a new study just needs to carry the metadata TSV.

TSV disambiguation — several "tsv/csv" artifacts are in play; this one is the
per-study metadata (per subject/session: ``datatypes``, ``t1w_num``, ``bold_num``),
NOT OpenNeuro's all-studies ``studies.tsv``, the campaign statefile
``sourcedata+derivatives.tsv``, or babs's in-project ``processing_inclusion.csv``.
"""

import csv
import io
import subprocess
import sys
from pathlib import Path

SUBJECTS_SESSIONS_TSV = "sourcedata/sourcedata+subjects+sessions.tsv"
SUBJECTS_TSV = "sourcedata/sourcedata+subjects.tsv"

# OpenNeuroStudies writes one metadata TSV per *study*, keyed by a `source_id`
# column, because a study may hold several source datasets. It spells "this dataset
# has no sessions" as this literal, not as an empty cell.
SOURCE_ID_COLUMN = "source_id"
SESSION_ID_COLUMN = "session_id"
NA = "n/a"


def safe_int(s):
    """Parse int from string; treat empty/invalid as 0."""
    try:
        return int(s)
    except (ValueError, TypeError):
        return 0


def read_study_metadata(study):
    """The cloned study's per-study metadata TSV text + its level ('session'|'subject').

    Prefers the sessions TSV (session-level); falls back to the subjects TSV. Reads
    the study's local file — ``datalad get`` fetches the content first if it's
    annexed and not present (a broken symlink).
    """
    study = Path(study)
    for rel, level in ((SUBJECTS_SESSIONS_TSV, "session"), (SUBJECTS_TSV, "subject")):
        path = study / rel
        if path.is_symlink() and not path.exists():  # annexed, content not present
            subprocess.run(["datalad", "get", "-d", str(study), str(path)], check=True)
        if path.is_file():
            return path.read_text(), level
    raise RuntimeError(
        f"no sourcedata metadata TSV in {study} "
        f"({SUBJECTS_SESSIONS_TSV} or {SUBJECTS_TSV})"
    )


def has_session(row):
    """True if this metadata row names a real session (not absent, not ``n/a``)."""
    return (row.get(SESSION_ID_COLUMN) or "").strip().lower() not in ("", NA)


def rows_for_source_dataset(tsv_text, source_id):
    """The metadata rows describing one source dataset. Raises if there are none.

    A study's metadata TSV covers every source dataset in it, keyed by ``source_id``.
    Two shapes are accepted, because both exist in the wild:

    - **no ``source_id`` column, or exactly one distinct value** — the study
      describes a single source dataset, so every row is that dataset's. This is what
      lets a generic sourcedata slot (``sourcedata/raw``) work: the directory name is
      not the dataset's id and must not be matched against one.
    - **several distinct values** — the study holds several source datasets, so
      ``source_id`` is matched against the sourcedata directory's name. A name that
      matches none is the user pointing at data the metadata does not describe, and
      is refused with the ids that *are* described.
    """
    rows = list(csv.DictReader(io.StringIO(tsv_text), delimiter="\t"))
    ids = {(r.get(SOURCE_ID_COLUMN) or "").strip() for r in rows}
    ids.discard("")
    if len(ids) > 1:
        rows = [r for r in rows if (r.get(SOURCE_ID_COLUMN) or "").strip() == source_id]
        if not rows:
            raise RuntimeError(
                f"the study metadata describes no source dataset {source_id!r} "
                f"(it describes: {', '.join(sorted(ids))})"
            )
    elif not rows:
        raise RuntimeError("the study metadata TSV has no rows")
    return rows


def summarize(rows):
    """The identity facts ``add-dataset`` records for a cell, from metadata rows.

    ``processing_level`` is read from the **data**, not from which file the metadata
    came in: a dataset is session-level exactly when its rows name real sessions.
    ``n_sessions`` counts sessions across all subjects (upstream's ``sessions_num``),
    and is blank rather than 0 for a subject-level dataset — the number is not
    applicable there, and a 0 would read as "sessions, none of them".
    """
    sessions = {(r["subject_id"], r[SESSION_ID_COLUMN]) for r in rows if has_session(r)}
    return {
        "processing_level": "session" if sessions else "subject",
        "n_subjects": str(len({r["subject_id"] for r in rows})),
        "n_sessions": str(len(sessions)) if sessions else "",
    }


def sniff_source_dataset(study, source_id):
    """Read the study's metadata TSV and summarize one source dataset in it.

    The study's own file: ``add-dataset`` selects data already present, so the
    summary is read from the study rather than fetched from the catalog it was
    cloned from. (``read_study_metadata`` still ``datalad get``s the TSV's content
    if the study annexed it — OpenNeuroStudies keeps it in git, so normally not.)
    """
    tsv_text, _ = read_study_metadata(study)
    return summarize(rows_for_source_dataset(tsv_text, source_id))


def build_eligibility(rule):
    """A predicate over an aggregated row, from a pipeline's ``selection:`` config:
    every ``require_datatypes`` present AND every ``require_positive`` count > 0.

    The rule names TSV columns directly (``t1w_num``, …), so a new app's needs are
    data, not code."""
    req_datatypes = rule.get("require_datatypes", [])
    req_positive = rule.get("require_positive", [])

    def eligible(agg):
        return all(dt in agg["datatypes"] for dt in req_datatypes) and all(
            agg["counts"].get(c, 0) > 0 for c in req_positive
        )

    return eligible


def aggregate(rows, level):
    """Merge rows sharing a ``(sub[,ses])`` key into one aggregate: union ``datatypes``,
    sum the ``*_num`` counts.

    Fixes #11: some studies split modalities across rows for the *same* key (one row
    ``anat``, another ``fmap,func``); a row-by-row filter never sees both at once.
    Aggregating first lets the rule see the whole (sub[,ses]).
    """
    groups = {}  # key -> {sub, ses, datatypes: set, counts: {col: int}}
    for r in rows:
        sub, ses = r["subject_id"], r.get("session_id", "")
        key = (sub, ses) if level == "session" else (sub,)
        g = groups.setdefault(
            key, {"sub": sub, "ses": ses, "datatypes": set(), "counts": {}}
        )
        g["datatypes"].update(t.strip() for t in r["datatypes"].split(",") if t.strip())
        for col, val in r.items():
            if col.endswith("_num"):
                g["counts"][col] = g["counts"].get(col, 0) + safe_int(val)
    return list(groups.values())


def generate_inclusion(tsv_text, rule, output, *, processing_level, limit=None):
    """Write an inclusion CSV of eligible subjects/sessions for a pipeline's ``rule``.

    Aggregates the TSV to ``processing_level`` (union datatypes, sum counts) BEFORE
    the eligibility check, sorts (so a ``limit`` cap is a reproducible "first N"),
    caps, and writes ``sub_id[,ses_id]``. Raises if session-level is asked of a
    subjects-only study, or if nothing is eligible.
    """
    reader = csv.DictReader(io.StringIO(tsv_text), delimiter="\t")
    rows = list(reader)
    if processing_level == "session" and "session_id" not in (reader.fieldnames or []):
        raise RuntimeError(
            "session-level requested but the study metadata is subjects-only"
        )

    is_eligible = build_eligibility(rule)
    eligible = sorted(
        (a for a in aggregate(rows, processing_level) if is_eligible(a)),
        key=lambda a: (a["sub"], a["ses"]),
    )
    if limit is not None:
        eligible = eligible[:limit]
    if not eligible:
        raise RuntimeError(f"no eligible subjects for selection rule {rule}")

    if processing_level == "session":
        fieldnames = ["sub_id", "ses_id"]
        out_rows = [{"sub_id": a["sub"], "ses_id": a["ses"]} for a in eligible]
    else:
        fieldnames = ["sub_id"]
        out_rows = [{"sub_id": a["sub"]} for a in eligible]

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)
    print(f"Wrote {len(out_rows)} rows to {output}", file=sys.stderr)
    return processing_level
