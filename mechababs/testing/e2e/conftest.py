"""pytest fixtures for the mechababs e2e harness.

Locally this runs INSIDE the pennlinc/slurm-docker-ci container (launched by
run_in_podman.sh); on a real cluster it will run on the login node. Either way the
scenario drives the real CLI against a real study, so the fixtures build the world
that CLI expects to already exist:

- `simbids_sif` — the simbids container, inside a plain ReproNim/containers clone
  seeded once in the workdir as host-prep, so the suite names the same kind of
  container dataset a production config does.
- `rawdata` — fake BIDS input, generated once into the workdir cache. Prod uses real
  OpenNeuro data, so fake input is a test-only concern that lives in the test, not in
  any prod tool.
- `study` — an OpenNeuroStudies-shaped study wrapping that raw data, cached and then
  copied per test. mechababs operates on a study that ALREADY EXISTS (docs/spec.md,
  "Layout & input") and never authors one, so the study is a fixture rather than
  something a mechababs verb produces. Building it here is a test fixture, not study
  authoring re-entering scope — the same carve-out the spec makes for `test-cluster`.

There is no campaign fixture: `mechababs campaign init` is the first thing the
scenario runs, so building a campaign is the code under test, not scaffolding
around it.
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

# The container dataset the suite's app configs name, as a workdir-local clone. The
# configs reach it as `../containers` relative to a study, so it has to sit beside
# the studies this suite builds.
CONTAINERS_DIRNAME = "containers"
CONTAINERS_URL = "https://github.com/ReproNim/containers.git"
SIMBIDS_IMAGE = "images/bids/bids-simbids--0.0.3.sif"

# babs main, not a release: the suite's app configs point at a native
# ReproNim/containers layout, which only `PennLINC/babs#399` resolves (merged to
# main, in no release yet). Drop this default back to None once a release carries it.
DEFAULT_BABS = "https://github.com/PennLINC/babs.git@main"

# What a scenario adds to a campaign's declaration to make its environment move.
# Deliberately dull: pure Python, no dependencies of its own, a universal wheel, and
# nothing in a campaign's tree pulls it in — so "is it importable from the campaign
# venv" is an unambiguous answer to "did the sync really run", and the resolve it
# provokes costs a second rather than a rebuild of the world.
BUMP_PACKAGE = "inflection"


def bump_declaration(campaign, package=BUMP_PACKAGE):
    """Hand-edit a campaign's ``pyproject.toml`` the way the docs say to bump one.

    There is no bump flag, by design: `update-env` re-resolves whatever the
    declaration now says, and the declaration is the user's own file. So a scenario
    that exercises the bump path has to edit that file the way a user does, in the
    text — anything else would test a code path no user takes.

    Refuses if the package is already declared, so a scenario cannot quietly assert
    nothing: the whole point is that the environment moves.
    """
    path = Path(campaign) / "pyproject.toml"
    text = path.read_text()
    assert f'"{package}"' not in text, (
        f"{package} is already declared in {path} — this bump would move nothing"
    )
    marker = "dependencies = [\n"
    assert marker in text, f"no dependency list to edit in {path}:\n{text}"
    path.write_text(text.replace(marker, f'{marker}    "{package}",\n', 1))
    return package


def pytest_addoption(parser):
    parser.addoption(
        "--cluster-config",
        default=None,
        help="REQUIRED. Path to the cluster config to validate. This is the "
        "cross-cluster axis — point it at a real site config to validate that config the "
        "same way. Always a resolved path: `campaign init --cluster` takes a path or URL "
        "and copies it in, so there is no bare name to look up.",
    )
    parser.addoption(
        "--mechababs",
        default=None,
        help="Optional `URL@REF` pinning the mechababs the campaigns record — passed "
        "straight to `campaign init --mechababs`. Omitted, `campaign init` pins "
        "whichever mechababs is running it (read from its PEP 610 install metadata), "
        "which is what a user's own `campaign init` does and what `test-cluster` "
        "relies on. A dev run passes its own checkout (run_in_podman.sh mounts it and "
        "hands over the mount path); `git clone` takes a local path, which is what "
        "makes an unpushed branch testable.",
    )
    parser.addoption(
        "--babs",
        default=None,
        help="Optional `URL@REF` pinning babs to a git checkout. Omitted, a campaign "
        "declares babs as a plain dependency and the lock freezes the latest PyPI "
        "release — which is what a user gets, so it is the default here too. Pass it "
        "to run the suite against an unmerged babs fix.",
    )


@pytest.fixture(scope="session")
def cluster_config(request):
    """The cluster config under test, as an absolute path.

    A UsageError rather than a skip: pytest exits 0 when every test skips, so a skip
    would let a validation run report success having validated nothing — the worst
    outcome for a validation command. The cluster config is the one option with no
    sane default, because it is the thing being validated.
    """
    value = request.config.getoption("--cluster-config")
    if not value:
        raise pytest.UsageError(
            "--cluster-config is required: it is the config under test. Pass the path "
            "to the cluster config you want validated."
        )
    path = Path(value).expanduser()
    if not path.is_file():
        raise pytest.UsageError(f"cluster config is not a file: {path}")
    return path.resolve()


@pytest.fixture(scope="session")
def mechababs_pin(request):
    """The `URL@REF` the scenario's campaigns pin, or ``None`` to let init self-pin.

    A dev run hands it in (the checkout under test is a mount path init could not
    infer); `test-cluster` leaves it unset, so `campaign init` pins the mechababs
    running it, which IS the code under test (see validate.py). No default of its
    own: unset means "omit the flag", not "guess".
    """
    return request.config.getoption("--mechababs")


@pytest.fixture(scope="session")
def babs_pin(request):
    """The `URL@REF` the scenario's campaigns pin for babs: `--babs`, else
    ``DEFAULT_BABS`` (see its comment for why main and not a release)."""
    return request.config.getoption("--babs") or DEFAULT_BABS


@pytest.fixture(scope="session")
def app_configs():
    """The suite's own test app configs, shipped beside it.

    The SimBIDS phantom configs are scenario fixtures, not starters a user would copy,
    so they travel with the suite instead of living in `examples/`. That also keeps the
    scenario runnable from an install, where the repo's `examples/` is absent.
    """
    path = Path(__file__).resolve().parent / "pipelines"
    if not path.is_dir():
        raise RuntimeError(f"suite app configs missing at {path} (incomplete install?)")
    return path


@pytest.fixture(scope="session")
def workdir():
    """Base dir where the studies and the container dataset live as siblings.

    Defaults to /scratch (the container's writable layer). On a real cluster, point
    it at scratch space via MECHABABS_E2E_WORKDIR — the app configs resolve the
    container dataset as `../containers` relative to a study, so the studies this
    suite builds and that clone must share a parent.
    """
    return Path(os.environ.get("MECHABABS_E2E_WORKDIR", "/scratch"))


@pytest.fixture(scope="session")
def simbids_sif(workdir):
    """Path to the simbids SIF, inside the workdir's ReproNim/containers clone.

    Upstream carries `bids-simbids--0.0.3.sif` itself, so the only host prep is a
    clone plus one `datalad get`. Local rather than the GitHub URL because babs
    installs `container.source` into every derivative it inits — clone once here,
    not once per cell.
    """
    sif = workdir / CONTAINERS_DIRNAME / SIMBIDS_IMAGE
    if not sif.exists():
        pytest.skip(
            f"simbids SIF missing at {sif} — seed the container dataset first:\n"
            f"    datalad clone {CONTAINERS_URL} {workdir / CONTAINERS_DIRNAME}\n"
            f"    datalad -C {workdir / CONTAINERS_DIRNAME} get {SIMBIDS_IMAGE}"
        )
    return sif


@pytest.fixture(scope="session")
def cache(workdir):
    """Where generated fixtures are cached between runs.

    Under the workdir, not beside this file: the suite ships inside the package, so
    a package-relative cache would write into the install tree (read-only for a
    normal install, and shared across campaigns for an editable one). The workdir is
    already the suite's scratch space, so the cache belongs there.
    """
    path = workdir / "e2e-cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def rawdata(simbids_sif, cache):
    """Fake BIDS input, generated once into the workdir cache (reused if present).

    Prod add-dataset's a real OpenNeuro URL, so fake input has no prod home — it's a
    test-only concern owned by the test. Generated via simbids-raw-mri inside the
    simbids container; datalad-ified so babs can clone it as raw input. Named by its
    accession (`ds999999`, like real OpenNeuro raw dirs) so the dataset id derives
    cleanly from its path.
    """
    dest = cache / DATASET_ID
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
        [
            "datalad",
            "save",
            "-d",
            str(dest),
            "-m",
            f"simbids phantom BIDS ({DATASET_ID})",
        ],
        check=True,
    )


@pytest.fixture(scope="session")
def study_template(rawdata, cache):
    """A fake OpenNeuroStudies-shaped study wrapping the phantom `rawdata`.

    mechababs operates on a study that already exists and never authors one, so dev
    needs a faithful stand-in for the `OpenNeuroStudies/study-ds<X>` prod clones:
    upstream's `dataset_description.json`, the per-subject metadata TSV that the
    add-dataset sniff reads, and the raw phantom registered as a real datalad
    SUBDATASET (`sourcedata/<id>`) rather than a plain dir — so the scenario runs
    against the nested-dataset structure derivatives land in.

    Built once into the workdir cache; `study` hands each test its own copy.
    """
    dest = cache / f"study-{DATASET_ID}"
    if not (dest / ".datalad").exists():
        _build_study(dest, rawdata)
    return dest


@pytest.fixture(scope="function")
def study(study_template, workdir):
    """A private copy of the template study, one per test.

    The scenario writes into the study — `campaign init` creates
    `.mechababs/campaigns/<label>/` and commits it, `add-dataset` commits statefile
    rows — so a shared study would leak state between tests and between runs. Copied
    rather than re-derived: generating the phantom means running the simbids
    container, which is the slow part of the whole suite.

    Under the workdir, so the copy lives on the same host bind mount as everything
    else and survives the run for inspection. `symlinks=True` keeps the raw data's
    annex symlinks as symlinks; the annex objects they point into are copied with them.
    """
    dest = workdir / f"e2e-study-{uuid.uuid4().hex[:8]}"
    shutil.copytree(study_template, dest, symlinks=True)
    log.info("study for this test: %s", dest)
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
        [
            "datalad",
            "save",
            "-d",
            str(dest),
            "-m",
            f"fake study-{DATASET_ID} wrapping the simbids phantom",
        ],
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
            w.writerow(
                {
                    "subject_id": sub.name,
                    "datatypes": ",".join(datatypes),
                    "t1w_num": len(list(sub.glob("anat/*_T1w.nii*"))),
                    "bold_num": len(list(sub.glob("func/*_bold.nii*"))),
                }
            )


def _write_study_description(path):
    """The study-level `dataset_description.json` — the upstream OpenNeuroStudies
    shape, which mechababs never authors or modifies in prod (it clones it). Here
    we synthesize the same shape so the fixture is faithful.
    """
    path.write_text(
        json.dumps(
            {
                "Name": f"study-{DATASET_ID}",
                "BIDSVersion": "1.9.0",
                "DatasetType": "study",
                "GeneratedBy": [{"Name": "openneuro-studies"}],
            },
            indent=2,
        )
        + "\n"
    )
