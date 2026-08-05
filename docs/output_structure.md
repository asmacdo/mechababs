# mechababs output structure

**The goal.** This is the **target** shape for the objects mechababs works on and produces: a **study**, the derivatives it gains, and — when many studies run together — the **superstudy** that groups them.
It is not what we produce today, and the study-first shape below is under active design.
Where today's output deviates from this target, there is an open issue for the gap — or this document is wrong and should change.

The whole document is open for feedback; 💬 marks the points that specifically need it — open questions we want others to weigh in on.

## Everything is a dataset

Every level that is a **dataset** here is a datalad dataset, and valid BIDS (`dataset_description.json` and `LICENSE` in each root) — except where noted, as possible future improvements to BIDS.
A campaign is *not* a dataset — it is a record kept inside a study (see below).

mechababs orchestrates babs, which **produces derivatives**.
It does not reshape data: a derivative is created **in its final home**, inside a study's `derivatives/`, and nothing is composed or relocated afterwards (this preserves clean `datalad run` provenance); publishing moves objects outward without reshaping them.

The **study is the primary unit.**
Today we rely on studies cloned from [OpenNeuroStudies](https://github.com/OpenNeuroStudies/OpenNeuroStudies), which already describe the raw data and any prior derivatives.
In principle mechababs can *operate* on any valid BIDS study, but 💬 **creating** one is a gap: authoring the study — from raw data, or by assembling assorted source datasets — and generating the metadata files it depends on (the per-subject datatypes/counts TSV that selection reads, and the catalogs) are today handled by OpenNeuro tooling, so this is a real barrier to entry for anyone outside that ecosystem.
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
    <Tool>-<Ver>+<stage>/      # a new babs derivative (see "a derivative dataset")
  .mechababs/                  # mechababs' record — bidsignored, NOT a dataset
    campaigns/
      <label>/                 # one campaign's footprint in this study
        campaign.yaml          #   the BIDS-App-config bundle (+ depends_on chain) + cluster choice
        bids-app-configs/      #   the individual app configs (mriqc, fmriprep-anat, ...)
        clusters/              #   cluster config(s)
        pyproject.toml         #   declares mechababs @ git+..., babs @ git+...
        uv.lock                #   the resolved, reproducible environment
        <statefile>            #   this study's cells for this campaign
    derivative-attempts/       # 💬 retired derivatives, kept for their evidence
```

A study can hold **more than one** `sourcedata/<id>`; a campaign selects which to process (the `(study, sourcedata)` pair is the coarse selection; subjects/configs are the fine selection).
Derivative directory names follow the upstream convention — `<Tool>-<Ver>` in the tool's own casing (`fMRIPrep-25.1.1`, `MRIQC-24.0.2`) — plus `+<stage>` where a run has stages (`fMRIPrep-25.1.1+anat`).

**A campaign is a config-epoch run, not a dataset.**
`.mechababs/campaigns/<label>/` is where a study records each campaign that touched it: one pinned environment (`uv.lock` fixes `mechababs` + `babs` by git commit — a fork is just a different URL), one bundle of BIDS-App configs, and the state of that study's cells.
A study **accumulates** campaigns over time — a set of derivatives now, another a year later with newer tools, each its own `<label>` — and because the record is the study's own, the study stays operable standalone: clone it, `uv sync` the campaign's lock, and everything needed to add to it or reproduce it is inside.

**`.mechababs/derivative-attempts/`** 💬 holds derivatives that had to be redone (a resource change, a tool bug, a config fix): `mechababs retire-derivative` moves the dataset here and resets its cell in one transition, so the evidence — logs, git history, `datalad run` records of *why* it was redone — is kept rather than deleted.
💬 **Open:** whether these should live inside the study at all — they would then travel with a published study, which we may not want. Needs discussion.

---

## `superstudy/` — an optional coordinator (a study of studies)

When a campaign runs across many studies, they are grouped in a **superstudy**: a study-of-studies, exactly the pattern [OpenNeuroStudies](https://github.com/OpenNeuroStudies/OpenNeuroStudies) already is (`DatasetType: study`, members at root).
The superstudy is a *pattern, not a fixed dataset* — OpenNeuroStudies can be one; a lab may have its own.

```
superstudy/
  dataset_description.json     # DatasetType "study"
  studies.tsv                  # 💬 catalog of member studies (+ studies.json sidecar)
  studies+derivatives.tsv      # 💬 map of which derivatives exist per study (OpenNeuroStudies' file)
  study-<id1>/                 # member studies, at root
  study-<id2>/
  .mechababs/
    campaigns/
      <label>/                 # a campaign authored here and fanned out to the members
        campaign.yaml          #   the bundle + `studies:` (inline list OR selected-studies.tsv)
        bids-app-configs/  clusters/  pyproject.toml  uv.lock
        selected-studies.tsv   #   the (study, sourcedata) subset this campaign runs on
```

A superstudy campaign dir mirrors a study's, with one complementary difference: it carries **membership** (`campaign.yaml`'s `studies:` field — an inline list *or* a `selected-studies.tsv`) and **no statefile** — the state shards to the member studies, and the superstudy computes the rollup from them.
So a study has `+state / −membership` (it *is* one study); a superstudy has `+membership / −state` (it coordinates many).

`mechababs campaign init` is the same command at either level.
At a superstudy it authors the config once and distributes a copy into each selected member's `.mechababs/campaigns/<label>/`.

**Homing a superstudy is optional.** The orchestration provenance lives in the studies (each study's git history, its `datalad run` records, its copied-down config), so the superstudy holds nothing durable the members don't — it can be re-derived from them. It *can* be published to a durable home (be OpenNeuroStudies, or a dedicated superdataset) but does not have to be.

💬 **`studies.tsv`** — this catalog name is overloaded: OpenNeuroStudies already has a `studies.tsv` and `studies+derivatives.tsv` (authoritative indexes). Whether the superstudy's catalog reuses that file or needs its own name is a question to raise. (The campaign's `selected-studies.tsv` is a distinct thing — a per-campaign subset, in `.mechababs/`, not the root catalog.)
💬 A study-of-studies is also a gap in BIDS — BIDS describes no study containing studies, though OpenNeuroStudies already uses the pattern. **TODO: raise with BIDS.**

---

## `<Tool>-<Ver>+<stage>/` — a derivative dataset

The unit of work, one per (source dataset × bids-app-config) cell, tracked by the study's statefile.
This is the babs project: `babs init` targets this path, and its root is the derivative's root.
The BIDS app writes `dataset_description.json` and `sub-*` here.

```
<Tool>-<Ver>+<stage>/          # e.g. fMRIPrep-25.1.1+anat
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

### `prov/` — orchestration provenance 💬

💬 This section is under active design — the shape below was not fully settled even before study-first, and study-first changes where the `Bundle` points: **to the study** (which now holds the orchestration record) rather than to a separate campaign dataset.

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
