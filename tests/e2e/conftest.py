"""pytest fixtures for the mechababs e2e harness.

Locally this runs INSIDE the pennlinc/slurm-docker-ci container (launched by
run_in_podman.sh); on a real cluster it runs on the login node. Either way the
scenario drives the campaign CLI, so the fixtures provide everything it needs:

- `simbids_sif` — the simbids container, from the shim built once as host-prep
  (`tmp-repronim-container-shim.sh bids-simbids`). This is a temporary seam: when
  babs#383 lands + simbids is upstreamed to ReproNim/containers, only this fixture
  changes (shim path -> ReproNim `datalad get`).
- `rawdata` — fake BIDS input, generated once into a gitignored repo cache. Prod
  uses real OpenNeuro data, so fake input is a test-only concern that lives in the
  test, not in any prod tool.
- `campaign` — the bootstrapped campaign env (provisioning), exercising prod's
  real bootstrap.sh construction path.
"""

import csv
import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

log = logging.getLogger("mechababs.e2e")

# The simbids-raw-mri config baked into the simbids container (a single-session,
# subject-level phantom dataset). It labels its phantom `ds005237` — a REAL
# OpenNeuro accession, and simbids is upstream (pennlinc/simbids), not ours to
# change — so the study fixture wraps it under the sentinel id below instead.
SIMBIDS_CONFIG = "ds005237_configs.yaml"

# The fixture's dataset id: an obviously-fake sentinel, chosen to NOT collide with
# any real OpenNeuro accession (unlike the phantom's own ds005237).
DATASET_ID = "ds999999"

# This repo (tests/e2e/conftest.py -> repo root) — the mechababs under test, which
# bootstrap.sh clones + pins into the campaign. No env var: pytest runs from here.
REPO = Path(__file__).resolve().parents[2]


def pytest_addoption(parser):
    parser.addoption(
        "--cluster-config",
        default="test-docker.yaml",
        help="cluster config name under examples/clusters/ — configure copies it into the "
        "campaign (default: test-docker.yaml). The cross-cluster axis: point at a real site "
        "config to validate it the same way.",
    )


@pytest.fixture(scope="session")
def cluster_config(request):
    return request.config.getoption("--cluster-config")


@pytest.fixture(scope="session")
def workdir():
    """Base dir where the campaign and the shim live as siblings.

    Defaults to /scratch (the container's writable layer, where run_in_podman.sh
    mounts the shim). On a real cluster, point it at scratch space via
    MECHABABS_E2E_WORKDIR — the pipeline resolves the shim as
    `../repronim-containers-shim`, so campaign and shim must share a parent.
    """
    return Path(os.environ.get("MECHABABS_E2E_WORKDIR", "/scratch"))


@pytest.fixture(scope="session")
def simbids_sif(workdir):
    """Path to the simbids SIF — the temporary shim seam.

    Today it lives in the shim built as host-prep by `tmp-repronim-container-shim.sh
    bids-simbids`. When babs#383 lands + simbids is upstreamed to ReproNim, this
    becomes a `datalad get` from ReproNim and nothing else changes.
    """
    sif = workdir / "repronim-containers-shim" / "images" / "bids" / "bids-simbids--0.0.3.sif"
    if not sif.exists():
        pytest.skip(
            f"simbids SIF missing at {sif} — build the shim first:\n"
            f"    REPRONIM={workdir}/repronim-containers-shim "
            f"tmp-repronim-container-shim.sh bids-simbids"
        )
    return sif


@pytest.fixture(scope="session")
def rawdata(simbids_sif):
    """Fake BIDS input, generated once into a gitignored repo cache (reused if present).

    Prod add-dataset's a real OpenNeuro URL, so fake input has no prod home — it's a
    test-only concern owned by the test. Generated via simbids-raw-mri inside the
    simbids container; datalad-ified so babs can clone it as raw input. Named by its
    accession (`ds999999`, like real OpenNeuro raw dirs) so the dataset id derives
    cleanly from its path.
    """
    dest = Path(__file__).resolve().parent / "_cache" / DATASET_ID
    if not (dest / ".datalad").exists():
        _generate_fake_bids(dest, simbids_sif)
    return dest


def _generate_fake_bids(dest, sif):
    log.info("generating fake BIDS at %s", dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    runner = shutil.which("apptainer") or shutil.which("singularity")
    assert runner, "need apptainer or singularity to generate fake BIDS"
    # simbids-raw-mri writes a `simbids/` subdir under its output dir; generate into
    # a scratch dir alongside the cache, then move it into place.
    gen = tempfile.mkdtemp(dir=dest.parent)
    subprocess.run(
        [runner, "exec", "-B", gen, str(sif), "simbids-raw-mri", gen, SIMBIDS_CONFIG],
        check=True,
    )
    shutil.move(f"{gen}/simbids", str(dest))
    shutil.rmtree(gen)
    subprocess.run(["datalad", "create", "--force", str(dest)], check=True)
    subprocess.run(
        ["datalad", "save", "-d", str(dest), "-m",
         f"simbids phantom BIDS ({DATASET_ID})"],
        check=True,
    )


@pytest.fixture(scope="session")
def study(rawdata):
    """A fake OpenNeuroStudies-shaped study wrapping the phantom `rawdata`.

    mechababs clones a study and `babs init`s the derivative into its
    `derivatives/`. Prod clones `OpenNeuroStudies/study-ds<X>`; dev has no such
    study, so we build a faithful one from the phantom raw data — the same shape a
    real clone would have, with no network. Built once into the gitignored repo
    cache, reused if present.

    The raw phantom is registered as a real datalad SUBDATASET (`sourcedata/<id>`),
    not a plain dir, so the fixture exercises the nested-dataset structure the
    campaign runs against (campaign -> study -> derivative).
    """
    dest = Path(__file__).resolve().parent / "_cache" / f"study-{DATASET_ID}"
    if not (dest / ".datalad").exists():
        _build_study(dest, rawdata)
    return dest


def _build_study(dest, rawdata):
    log.info("building fake study at %s", dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # The study is itself a datalad dataset. text2git keeps the metadata (the
    # dataset_description + the subjects TSV) in git, not annex, so it travels with
    # a no-content clone (as real OpenNeuroStudies studies do) — else add-dataset's
    # clone gets broken annex symlinks.
    subprocess.run(["datalad", "create", "-c", "text2git", str(dest)], check=True)
    # sourcedata/<id> = the phantom raw, cloned in and registered as a subdataset.
    src = dest / "sourcedata" / DATASET_ID
    subprocess.run(
        ["datalad", "clone", "--dataset", str(dest), str(rawdata), str(src)],
        check=True,
    )
    _write_subjects_tsv(dest / "sourcedata" / "sourcedata+subjects.tsv", src)
    _write_study_description(dest / "dataset_description.json")
    subprocess.run(
        ["datalad", "save", "-d", str(dest), "-m",
         f"fake study-{DATASET_ID} wrapping the simbids phantom"],
        check=True,
    )


def _write_subjects_tsv(path, raw):
    """The per-subject metadata `select` reads: subject_id, datatypes, t1w_num,
    bold_num (the columns its eligibility filters key on). Derived by scanning the
    raw BIDS — annexed files show as symlinks, so globbing by name counts them
    without fetching content.
    """
    subs = sorted(p for p in raw.iterdir() if p.name.startswith("sub-"))
    fieldnames = ["subject_id", "datatypes", "t1w_num", "bold_num"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t")
        w.writeheader()
        for sub in subs:
            datatypes = sorted(d.name for d in sub.iterdir() if d.is_dir())
            w.writerow({
                "subject_id": sub.name,
                "datatypes": ",".join(datatypes),
                "t1w_num": len(list(sub.glob("anat/*_T1w.nii*"))),
                "bold_num": len(list(sub.glob("func/*_bold.nii*"))),
            })


def _write_study_description(path):
    """The study-level `dataset_description.json` — the upstream OpenNeuroStudies
    shape, which mechababs never authors or modifies in prod (it clones it). Here
    we synthesize the same shape so the fixture is faithful.
    """
    path.write_text(json.dumps({
        "Name": f"study-{DATASET_ID}",
        "BIDSVersion": "1.9.0",
        "DatasetType": "study",
        "GeneratedBy": [{"Name": "openneuro-studies"}],
    }, indent=2) + "\n")


@pytest.fixture(scope="function")
def campaign(workdir):
    """A freshly bootstrapped campaign env (provisioning), one per test.

    Calls the real bootstrap.sh — prod's exact construction path — vendoring this
    repo (at its current branch) as the mechababs under test; it clones, so only
    committed work is under test and a dirty tree is refused. Function-scoped so each
    test gets its own campaign (tests `configure` it, and `configure` refuses an
    existing ledger — a shared campaign would collide). The unique per-call name keeps
    bootstrap's refuse-existing-dir guard happy and avoids clobbering a prior run;
    clean up stale ones with `rm -rf $MECHABABS_E2E_WORKDIR/test-campaign-*`.

    --system-site-packages is opt-in via MECHABABS_E2E_SYSTEM_SITE_PACKAGES (set by
    run_in_podman.sh for the CentOS7 container, whose 2015 toolchain can't build the
    newest wheels); a real cluster leaves it unset and builds prod's isolated venv.
    """
    dirty = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain"],
        check=True, text=True, capture_output=True,
    ).stdout.strip()
    if dirty:
        pytest.fail(
            "repo is dirty; bootstrap clones the committed branch, so this run would "
            "test your last commit and silently ignore the working tree:\n" + dirty
        )
    ref = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True, text=True, capture_output=True,
    ).stdout.strip()
    path = workdir / f"test-campaign-{uuid.uuid4().hex[:8]}"
    cmd = [f"{REPO}/bootstrap.sh", str(path), "--mechababs", f"{REPO}@{ref}"]
    if os.environ.get("BABS_SPEC"):
        cmd += ["--babs", os.environ["BABS_SPEC"]]
    if os.environ.get("MECHABABS_E2E_SYSTEM_SITE_PACKAGES"):
        cmd.append("--system-site-packages")
    log.info("bootstrapping campaign at %s", path)
    subprocess.run(cmd, check=True)
    # No config staging here: the tests point configure at the vendored examples/
    # paths, exercising its copy-in (the prod path — a user points --cluster/--pipelines
    # at a config and configure copies it into the campaign's clusters/ + pipelines/).
    return path
