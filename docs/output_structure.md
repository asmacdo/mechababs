# mechababs output structure

**The goal.** This is the **target** shape for the objects mechababs works on and produces: a **study**, the derivatives it gains, and — when many studies run together — the **superstudy** that groups them.
The study and superstudy layout below is what mechababs produces today; the `prov/` record is not yet.
The words used here are defined in the [glossary](glossary.md).
Where today's output deviates from this target, there is an open issue for the gap — or this document is wrong and should change.

## Everything is a dataset

Every level that is a **dataset** here is a datalad dataset, and valid BIDS (`dataset_description.json` and `LICENSE` in each root) — except where noted, as possible future improvements to BIDS.
A campaign is *not* a dataset — it is a record kept inside a study (see below).

mechababs orchestrates babs, which **produces derivatives**.
It does not reshape data: a derivative is created **in its final home**, inside a study's `derivatives/`, and nothing is composed or relocated afterwards (this preserves clean `datalad run` provenance); publishing moves objects outward without reshaping them.

The **study is the primary unit.**
Today we rely on studies cloned from [OpenNeuroStudies](https://github.com/OpenNeuroStudies/OpenNeuroStudies), which already describe the raw data and any prior derivatives.
In principle mechababs can *operate* on any valid BIDS study, but **creating** one is a gap: authoring the study — from raw data, or by assembling assorted source datasets — and generating the metadata files it depends on (the per-subject datatypes/counts TSV that selection reads, and the catalogs) are today handled by OpenNeuro tooling, so this is a real barrier to entry for anyone outside that ecosystem.
Study authoring stays out of mechababs' scope; the answer is a shared study template or generator to recommend (e.g. [brain-bbqs/study-template](https://github.com/brain-bbqs/study-template)), and settling on one is open work.
A single study is fully operable on its own: no campaign or superstudy required.
The superstudy is an optional layer for running many studies at once.

---

## `study-<id>/` — the primary unit

A bids-study: a raw dataset (or several) grouped with the derivatives made from it.
mechababs' change is additive — a new derivative under `derivatives/`, and its own orchestration record under `.mechababs/` — and it never modifies what upstream authored (the `dataset_description.json`, `README`, existing derivative links).

```
study-<id>/
  dataset_description.json     # DatasetType "study"; authored upstream (or at creation)
  README.md
  sourcedata/<id>/             # submodule -> raw BIDS dataset  (a study may hold more than one)
  derivatives/
    <Tool>-<Ver>/              # pre-existing derivatives (any tool)
    <Tool>-<Ver>+<stage>+<id>+<label>/  # a new babs derivative (see "a derivative dataset")
  .mechababs/                  # mechababs' record — bidsignored, NOT a dataset
    campaigns/
      <label>/                 # one campaign's record in this study
        campaign.yaml          #   the BIDS-App-config bundle (+ depends_on chain) + cluster choice
        bids-app-configs/      #   the individual app configs (mriqc, fmriprep-anat, ...)
        clusters/              #   cluster config(s)
        env.sh                 #   source to select this campaign + activate its venv
        pyproject.toml         #   declares mechababs @ git+..., babs @ git+...
        uv.lock                #   the resolved, reproducible environment
        sourcedata+derivatives.tsv  # the statefile: this study's cells for this campaign
        inclusions/            #   the requested subject list per cell, pinned at scaffold
```

A study can hold **more than one** `sourcedata/<id>`; a campaign selects which to process (the `(study, sourcedata)` pair is the coarse selection; subjects/configs are the fine selection).
When the study holds exactly one raw BIDS dataset, the generic slots `sourcedata/raw` or `sourcedata/rawbids` are preferred; `sourcedata/<id>` covers the multiple-datasets case.
Derivative directory names follow the upstream convention — `<Tool>-<Ver>` in the tool's own casing (`fMRIPrep-25.1.1`, `MRIQC-24.0.2`) — plus `+<stage>` where a run has stages (`fMRIPrep-25.1.1+anat`).
Unless the sourcedata slot is a generic one (`sourcedata/raw`, `sourcedata/rawbids`) the derivative also carries the source-dataset id, since a cell is (source dataset × app config) and the name would collide when a study holds several source datasets; and it always ends in the campaign label, since a study accumulates campaigns and a new one must be able to produce the same cell beside the old.
So a mechababs derivative is `<Tool>-<Ver>+<stage>[+<id>]+<label>/` (e.g. `fMRIPrep-25.1.1+anat+ds000001+c1`).

**A campaign is a config-epoch run, not a dataset.**
`.mechababs/campaigns/<label>/` is where a study records each campaign that touched it: one pinned environment (`uv.lock` fixes `mechababs` + `babs` by git commit — a fork is just a different URL), one bundle of BIDS-App configs, and the state of that study's cells.
A study **accumulates** campaigns over time — a set of derivatives now, another a year later with newer tools, each its own `<label>` — and because the record is the study's own, the study stays operable standalone: clone it, `uv sync` the campaign's lock, and everything needed to add to it or reproduce it is inside.

**Retired derivatives live outside the study.**
A derivative that had to be redone (a resource change, a tool bug, a config fix) is retired, not deleted: `mechababs retire-derivative` moves it to a required target directory that must be **outside the study** (a destination inside is refused) and resets its cell in the same transition.
The evidence — logs, git history, `datalad run` records of *why* it was redone — is kept, and because the archive is outside the study, retired attempts never travel with a published study.

---

## `superstudy/` — an optional coordinator (a study of studies)

When a campaign runs across many studies, they are grouped in a **superstudy**: a study-of-studies, exactly the pattern [OpenNeuroStudies](https://github.com/OpenNeuroStudies/OpenNeuroStudies) already is (`DatasetType: study`, members at root).
The superstudy is a *pattern, not a fixed dataset* — OpenNeuroStudies can be one; a lab may have its own.

```
superstudy/
  dataset_description.json     # DatasetType "study"
  studies.tsv                  # OpenNeuroStudies' own catalog (authored upstream, when present)
  studies+derivatives.tsv      # OpenNeuroStudies' own derivative map (authored upstream, when present)
  study-<id1>/                 # member studies, at root
  study-<id2>/
  .mechababs/
    campaigns/
      <label>/                 # a campaign configured at this level
        campaign.yaml          #   the BIDS-App-config bundle + cluster choice
        bids-app-configs/  clusters/  env.sh  pyproject.toml  uv.lock
        studies+sourcedata.tsv #   membership: the (study, sourcedata) pairs this campaign runs on
```

A superstudy campaign dir mirrors a study's, with one complementary difference: it carries **membership** (`studies+sourcedata.tsv`) and **no statefile** — detailed per-cell state shards to the member studies, and the superstudy computes the rollup from them on demand.
So a study has `+state / −membership` (it *is* one study); a superstudy has `+membership / −state` (it coordinates many).
The one committed state at the super is deliberately coarse: a per-member lifecycle status (`registered` / `active` / `merged`) alongside the membership, recomputed from the member's shard on each tick that scaffolds or merges one of its cells and committed only when it changed — never a commit of its own.

`mechababs campaign init` is the same command at either level.
At a superstudy it touches only the superstudy's own campaign dir — no member studies are selected yet, so there is nothing to fan out to.
A member study's campaign dir (config copy, lock, empty statefile shard) is written when `add-dataset` first selects a source dataset in it; a member study added later gets its campaign dir the same way, so there is no separate catch-up step.

**Homing a superstudy is optional.** The orchestration provenance lives in the studies (each study's git history, its `datalad run` records, its copied-down config), so the superstudy holds nothing durable the member studies don't — it can be re-derived from them. It *can* be published to a durable home (be OpenNeuroStudies, or a dedicated superdataset) but does not have to be.

The root `studies.tsv` / `studies+derivatives.tsv` are OpenNeuroStudies' own authoritative indexes, present when the superstudy is OpenNeuroStudies (or another catalog-keeping superdataset); mechababs never writes them.
mechababs' catalog is the campaign's `studies+sourcedata.tsv` under `.mechababs/` — a distinct file with a distinct owner, so the names do not collide.
A study-of-studies is also a gap in BIDS — BIDS describes no study containing studies, though OpenNeuroStudies already uses the pattern. **TODO: raise with BIDS.**

---

## `<Tool>-<Ver>+<stage>[+<id>]+<label>/` — a derivative dataset

The unit of work, one per (source dataset × bids-app-config) cell, tracked by the study's statefile.
This is the babs project: `babs init` targets this path, and its root is the derivative's root.
The BIDS app writes `dataset_description.json` and `sub-*` here.

```
<Tool>-<Ver>+<stage>[+<id>]+<label>/  # e.g. fMRIPrep-25.1.1+anat+ds000001+c1; the id only when the source dataset is named
  dataset_description.json     # DatasetType "derivative"; GeneratedBy [<bids_app>]
  .bidsignore                  # containers/, logs/, prov/
  sub-*                        # unzipped derivative content
  prov/                        # not valid BIDS today — see below
  code/                        # babs scaffold: run script, config, inclusion
  containers/                  # submodule — the image that ran
  logs/
  sourcedata/raw/              # submodule -> the raw BIDS dataset
  .babs/                       # babs config + RIA stores (git-ignored)
```

Inputs are registered by **URL**, not local path, so the recorded provenance re-resolves anywhere.

### `prov/` — orchestration provenance

This section is under active design and **not yet produced**: the `datalad run` command capture in the study is the orchestration provenance mechababs delivers today, and the BEP028 record below is tracked separately.
The `Bundle` points **to the study**, which holds the orchestration record.

The BIDS app records itself in its own `dataset_description.json`. `prov/` records the tools that *composed and ran* it,
following [BEP028 / BIDS-Prov](https://github.com/bids-standard/BEP028_BIDSprov): `prov/prov-<label>_<suffix>.json`.
Records are written at init, from facts known at scaffold time; the app's outputs are never modified afterwards.

`prov/prov-mechababs_base.json` — context, and the link to the **study** that holds the orchestration record:

```jsonc
{
  "@context": "https://purl.org/nidash/bidsprov/context.json",
  "BIDSProvVersion": "0.0.1",

  // The study is LINKED, not copied: it holds the orchestration's full git
  // history, its `datalad run` records, the campaign's pinned env + config
  // under `.mechababs/campaigns/<label>/` — the real provenance, which a
  // summary could only approximate.
  //
  // `Bundle` is W3C PROV's term for "a named set of provenance descriptions,
  // and is itself an entity, so allowing provenance of provenance to be
  // expressed" (PROV-DM §5.4, https://www.w3.org/TR/prov-dm/#component4).
  // BIDS-Prov has no record type for a reference to provenance held in another
  // dataset; this is carried pending an answer.
  "Bundle": [
    {
      // The study's datalad-id: stable identity, the same across every commit —
      // it says WHICH study, never which state of it.
      "Id": "urn:uuid:a4c32684-d47e-4133-9e9e-29c8bc8f44c1",
      "Label": "mechababs study: study-ds<XXXXXX>",
      "AtLocation": "https://github.com/OpenNeuroStudies/study-ds<XXXXXX>.git",
      // The commit pins the state. This is the campaign-init (DEPLOY) commit:
      // the study version that fixed the campaign's config, env pins, and
      // selection for this run. It necessarily predates the commit that ingests
      // the merged result, so the pointer always references an earlier immutable
      // version and the graph is acyclic by construction — the study pins the
      // derivative as a subdataset, so the derivative cannot contain the study's
      // ingest sha. This derivative's orchestration is then `git log -- <its
      // path>` in the study; orchestration events are sparse, so the walk is cheap.
      "Digest": { "sha1": "9f3c1a2b7e4d5a6c8b0f1e2d3c4b5a6978e0f1a2" }
    }
  ]
}
```

`prov/prov-mechababs_soft.json` — the tools that composed and executed the run:

```jsonc
{
  "Records": {
    "Agent": [
      {
        "Id": "bids::prov/#mechababs",
        "Label": "mechababs",
        "Version": "0.1.dev42+g9f3c1a2"   // commit-bearing, so the exact code is recoverable
      },
      {
        "Id": "bids::prov/#babs",
        "Label": "BABS",
        // BIDS-Prov's Agent record has no field for a source repository. The
        // commit disambiguates the code; the fork it came from does not.
        "Version": "0.1.dev674+g07d0a80"
      }
    ]
  }
}
```

**Commands.** Each job's invocation is recorded twice over by babs, inside the derivative: `code/participant_job.sh` holds the `singularity run` command,
and the `datalad run` records in git history bind each command to the inputs it
consumed and the outputs it produced. The commands that *drove* those jobs — `campaign init`, `iterate`, their arguments and
timings — live in the study's run records, reached through the `Bundle` link.

BIDS-Prov serializes this as `Activity` records, which map closely onto `datalad run` records:

```jsonc
{
  "Records": {
    "Activity": [
      {
        "Id": "bids::prov/#fmriprep-sub-0001-ses-01",
        "Label": "fMRIPrep anatomical workflow, sub-0001 ses-01",
        "Command": "singularity run ...",
        "AssociatedWith": "bids::prov/#babs",
        "Used": ["bids::sourcedata/raw"],
        "StartedAtTime": "2026-07-15T16:11:04",
        "EndedAtTime": "2026-07-15T16:11:29"
      }
    ]
  }
}
```

---

## Publishing

Each object goes to its own home:

- the **derivative** → its own `OpenNeuroDerivatives/ds<XXXXXX>-<tool>` repository, standing alone;
- the **study**, with the new derivative registered under `derivatives/`, → `OpenNeuroStudies/study-ds<XXXXXX>`;
- the **superstudy** → optionally its own durable home, or none — it is re-derivable from the studies.

Because a derivative is published standalone, everything needed to interpret it travels inside it.
