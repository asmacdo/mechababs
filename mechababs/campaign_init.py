"""campaign_init.py — the body of ``mechababs campaign init``.

Creates a campaign inside an existing study: copies the user's app + cluster
configs in, declares and resolves the environment that will run them, builds that
environment, and writes the empty statefile the reconciler fills.

This is the one command that runs *before* the campaign environment exists, so it
is the one command that may run from anywhere — typically ephemerally, straight
from a pinned ref::

    uvx --from git+https://github.com/con/mechababs@v0.2 \\
        mechababs campaign init nprep --apps mriqc.yaml,fmriprep-anat.yaml \\
                                      --cluster dartmouth.yaml

Everything after it runs from the venv this builds.

**The lock is the provenance.** ``uv lock`` resolves ``mechababs`` and ``babs`` to
exact commits and writes them into ``uv.lock``, which is committed to the study.
That file — not a vendored code clone — is what says which tools ran, and a mid-campaign
version bump is an edit to it, with its git history as the record of the campaign's
config epochs. The mechababs pin is read from the running install (PEP 610
``direct_url.json``), so the campaign records the mechababs the user actually
invoked rather than a ref they would have to retype; babs, which mechababs only
shells out to, is named by ``--babs URL@REF``.
"""

import json
import re
import shutil
import sys
import urllib.parse
import urllib.request
from importlib import metadata
from pathlib import Path

import yaml

from mechababs import campaign as campaign_mod
from mechababs.utils import datalad_save, run

BABS_DEFAULT = "https://github.com/PennLINC/babs.git@main"

# Runtime tools a campaign needs beyond mechababs + babs themselves — the same set
# requirements-campaign.txt installs into a bootstrap-built venv. Kept as a literal
# because this command may run from an ephemeral uvx install, which has the
# mechababs *package* but not the repo file.
CAMPAIGN_EXTRAS = [
    "con-duct",     # usage/resource logs alongside every run
    "visidata",     # interactive TSV viewer for the statefile
    "pytest",       # runs the packaged e2e scenario behind `mechababs test-cluster`
]

# A label names a directory and is exported as an env var, so keep it boring.
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

UV = "uv"


# --------------------------------------------------------------------------
# Pinning the tools
# --------------------------------------------------------------------------

def parse_source_spec(spec, what):
    """Split a ``URL@REF`` pin. ``URL`` is anything git clones, a local path included.

    The ``URL@REF`` shape (and the requirement that ``REF`` be given rather than
    defaulted) is carried over from ``bootstrap.sh``: naming the ref explicitly is
    what makes "run a campaign against this PR branch" a config change instead of a
    code change.
    """
    url, sep, ref = spec.rpartition("@")
    if not sep or not url or not ref:
        sys.exit(f"--{what} expects URL@REF (e.g. "
                 f"https://github.com/PennLINC/babs.git@main), got: {spec}")
    return url, ref


def git_source(url, ref):
    """A ``[tool.uv.sources]`` entry for a git checkout at ``ref``.

    A local path becomes a ``file://`` URL, so a branch that exists only on disk can
    be run through a whole campaign before it is pushed anywhere (at the cost of a
    pin that resolves nowhere else — the accepted dev-mode trade).
    """
    url = url.removeprefix("git+")
    local = Path(url)
    if local.exists():
        url = local.resolve().as_uri()
    return {"git": url, "rev": ref}


def running_mechababs_pin():
    """How to pin the mechababs that is running: ``(requirement, source_or_None)``.

    Read from PEP 610 ``direct_url.json``, which uv and pip write for every
    non-registry install:

    - installed from git (the ``uvx --from git+…`` case) -> pinned by the **resolved
      commit**, not the branch name, so the campaign records exactly what ran;
    - installed from a local dir (a dev checkout) -> pinned by path, editable
      preserved. Honest rather than reproducible elsewhere — dev mode's known cost;
    - installed from a registry (a future PyPI release) -> a plain ``==`` version.
    """
    try:
        dist = metadata.distribution("mechababs")
    except metadata.PackageNotFoundError:      # running from a bare checkout
        return "mechababs", None
    raw = dist.read_text("direct_url.json")
    if not raw:
        return f"mechababs=={dist.version}", None
    direct = json.loads(raw)
    url = direct.get("url", "")
    if "vcs_info" in direct:
        vcs = direct["vcs_info"]
        return "mechababs", {"git": url, "rev": vcs.get("commit_id")
                             or vcs.get("requested_revision")}
    if "dir_info" in direct:
        path = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path))
        source = {"path": str(path)}
        if direct["dir_info"].get("editable"):
            source["editable"] = True
        return "mechababs", source
    return "mechababs", {"url": url}          # a wheel/sdist by URL


# --------------------------------------------------------------------------
# Staging the user's configs
# --------------------------------------------------------------------------

def stage_config(dest_dir, arg, what):
    """Copy one config into the campaign; return its filename.

    App and cluster configs are **user-provided**, given by path or URL and copied
    in — never a bare name resolved against a directory mechababs knows about. The
    copy is the point: the config that produced a run is committed in the study, so
    the run reproduces from the study alone.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    parsed = urllib.parse.urlparse(arg)
    if parsed.scheme in ("http", "https"):
        name = Path(parsed.path).name
        if not name:
            sys.exit(f"{what} config URL has no filename: {arg}")
        dest = dest_dir / name
        print(f"+ fetch {arg} -> {dest}", file=sys.stderr)
        with urllib.request.urlopen(arg) as response:      # http/https only, checked above
            dest.write_bytes(response.read())
        return name
    source = Path(arg)
    if not source.is_file():
        sys.exit(f"{what} config not found: {arg}\n"
                 f"App and cluster configs are given by path or URL — mechababs "
                 f"ships examples/ as starters to copy, not a library to name.")
    dest = dest_dir / source.name
    shutil.copy(source, dest)
    return source.name


def app_name(filename):
    """An app config's identity: its filename stem (``fMRIPrep-25.2.5+anat``).

    The same identity the derivative directory and the statefile's ``app_config``
    column carry. No declared key — the filename IS the name.
    """
    return Path(filename).stem


def declared_depends_on(config_path):
    """The app config's ``mechababs.depends_on``, or ``""``.

    mechababs-owned and deliberately separate from babs's ``input_datasets``:
    orchestration topology is mechababs's, run-wiring is babs's, and a gate-type
    dependency (mriqc gating fmriprep) is never an input at all.
    """
    config = yaml.safe_load(Path(config_path).read_text()) or {}
    return ((config.get("mechababs") or {}).get("depends_on") or "")


def resolve_apps(dest_dir, app_args):
    """Stage the app bundle; return ordered ``[(filename, name, depends_on), …]``.

    Duplicate names are rejected **before** anything is copied, so a rejected bundle
    never half-populates the campaign. A ``depends_on`` naming an app outside this
    bundle is rejected too: the edge could never resolve, and catching it here beats
    surfacing it once per dataset at ``add-dataset``.
    """
    names = [app_name(Path(urllib.parse.urlparse(a).path or a).name) for a in app_args]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        sys.exit(f"duplicate app config(s): {', '.join(sorted(duplicates))} — "
                 f"each app in a campaign needs a distinct name (the filename stem)")

    apps = []
    for arg in app_args:
        filename = stage_config(dest_dir, arg, "app")
        name = app_name(filename)
        apps.append((filename, name, declared_depends_on(dest_dir / filename)))

    known = {name for _, name, _ in apps}
    for _, name, upstream in apps:
        if upstream and upstream not in known:
            sys.exit(f"app {name!r} declares depends_on: {upstream!r}, which is not "
                     f"in this campaign ({', '.join(sorted(known))})")
    return apps


# --------------------------------------------------------------------------
# The campaign environment
# --------------------------------------------------------------------------

def _toml_str(value):
    return json.dumps(str(value))       # TOML basic strings are JSON-compatible here


def _toml_inline(source):
    # json.dumps renders both the strings and the `editable = true` bool as TOML.
    return "{ " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in source.items()) + " }"


def render_pyproject(label, mechababs_req, mechababs_source, babs_source):
    """The campaign's dependency declaration — a uv *virtual* project.

    No ``[build-system]``: the campaign is not a package to build, it is a set of
    pinned dependencies for ``uv lock`` / ``uv sync`` to resolve and install.
    """
    deps = [mechababs_req, "babs", *CAMPAIGN_EXTRAS]
    sources = {"babs": babs_source}
    if mechababs_source:
        sources["mechababs"] = mechababs_source

    lines = [
        f"# The environment for mechababs campaign {label!r}.",
        "#",
        "# Generated by `mechababs campaign init`, then resolved into uv.lock — the",
        "# lock is the campaign's provenance record of which mechababs + babs ran.",
        "# Edit and re-lock (`mechababs campaign update-env`) to bump mid-campaign:",
        "# completed cells keep the lock that produced them, new ones run at the new one.",
        "",
        "[project]",
        f"name = {_toml_str('mechababs-campaign-' + label)}",
        'version = "0"',
        'requires-python = ">=3.10"',
        "dependencies = [",
    ]
    lines += [f"    {_toml_str(d)}," for d in deps]
    lines += ["]", "", "[tool.uv.sources]"]
    lines += [f"{name} = {_toml_inline(source)}" for name, source in sources.items()]
    return "\n".join(lines) + "\n"


def build_env(campaign, label):
    """Resolve the campaign's lock and build its venv from it; stamp the venv.

    ``uv lock`` pins every dependency (the git refs to commits) and ``uv sync``
    installs exactly that — so the environment and the committed lock agree by
    construction, which is what the env-match guard later checks.
    """
    run(UV, "lock", "--project", str(campaign))
    run(UV, "sync", "--project", str(campaign), "--frozen")
    venv = campaign / campaign_mod.VENV_DIRNAME
    campaign_mod.write_env_stamp(
        venv, label, (campaign / campaign_mod.LOCK_FILENAME).read_text())
    return venv


ENV_SH_TEMPLATE = """\
# mechababs campaign {label!r} — SOURCE this file, don't execute it:
#
#     source {rel}
#
# It does the two things every mechababs command needs, together so they cannot
# disagree: selects this campaign (there is one venv per campaign, and selection is
# always explicit — no default-if-only-one) and activates the venv its uv.lock
# built. `deactivate` leaves the venv; MECHABABS_CAMPAIGN stays set until you unset it.
#
# Committed on purpose: the venv path is derived from this file's own location, so
# nothing here is specific to one machine.
if [ -n "${{BASH_SOURCE:-}}" ]; then
    _mechababs_self="${{BASH_SOURCE}}"
else
    _mechababs_self="$0"                      # zsh, dash: $0 is the sourced file
fi
_mechababs_campaign="$(cd "$(dirname "$_mechababs_self")" && pwd)"

export MECHABABS_CAMPAIGN={label_sh}
. "$_mechababs_campaign/{venv}/bin/activate"

unset _mechababs_self _mechababs_campaign
"""


def write_env_sh(campaign, label):
    """The one-step select-and-activate script (committed; see the template)."""
    path = campaign / campaign_mod.ENV_FILENAME
    path.write_text(ENV_SH_TEMPLATE.format(
        label=label,
        label_sh=f"'{label}'",
        rel=f"{campaign_mod.MECHABABS_DIR}/{campaign_mod.CAMPAIGNS_DIRNAME}/"
            f"{label}/{campaign_mod.ENV_FILENAME}",
        venv=campaign_mod.VENV_DIRNAME,
    ))
    return path


# --------------------------------------------------------------------------

def init(study, label, app_args, cluster_arg, *, limit=None,
         babs_spec=BABS_DEFAULT, mechababs_spec=None):
    """Create campaign ``label`` in ``study``. Returns the campaign directory.

    Writes only under ``.mechababs/campaigns/<label>/`` — mechababs' change to a
    study is additive, and never touches what upstream authored.
    """
    if not LABEL_RE.match(label):
        sys.exit(f"invalid campaign label {label!r} — it names a directory and is "
                 f"exported as an env var; use letters, digits, '.', '_', '-'")
    campaign = campaign_mod.campaign_dir(study, label)
    if campaign.exists():
        sys.exit(f"campaign {label!r} already exists: {campaign}\n"
                 f"A campaign is a config epoch — start another one under a new "
                 f"label rather than editing this one's identity.")
    if not app_args:
        sys.exit("--apps must name at least one BIDS-App config")

    campaign.mkdir(parents=True)

    # The venv is ephemeral and rebuilt from the lock; the flock is a runtime
    # artifact. Ignore both from INSIDE the campaign dir, so mechababs' whole
    # footprint stays under .mechababs/ and the study's own .gitignore is left alone.
    # Untracked-but-not-ignored files here would dirty the study, which the
    # transition verbs' clean-in guard reads as unattributable work.
    (campaign / ".gitignore").write_text(
        f"{campaign_mod.VENV_DIRNAME}/\n{campaign_mod.FLOCK_FILENAME}\n")

    apps = resolve_apps(campaign / campaign_mod.APPS_DIRNAME, app_args)
    cluster_file = stage_config(
        campaign / campaign_mod.CLUSTERS_DIRNAME, cluster_arg, "cluster")

    config = {
        "label": label,
        "apps": [f"{campaign_mod.APPS_DIRNAME}/{filename}" for filename, _, _ in apps],
        "cluster": f"{campaign_mod.CLUSTERS_DIRNAME}/{cluster_file}",
        "limit": limit,
    }
    (campaign / campaign_mod.CONFIG_FILENAME).write_text(
        yaml.safe_dump(config, sort_keys=False))

    # Header only: which source datasets a campaign acts on is an explicit
    # selection, made by `add-dataset`, not implied by init.
    (campaign / campaign_mod.STATE_FILENAME).write_text(campaign_mod.initial_header())

    if mechababs_spec:
        mechababs_req, mechababs_source = "mechababs", git_source(
            *parse_source_spec(mechababs_spec, "mechababs"))
    else:
        mechababs_req, mechababs_source = running_mechababs_pin()
    babs_source = git_source(*parse_source_spec(babs_spec, "babs"))
    (campaign / campaign_mod.PYPROJECT_FILENAME).write_text(
        render_pyproject(label, mechababs_req, mechababs_source, babs_source))

    write_env_sh(campaign, label)
    build_env(campaign, label)

    datalad_save(
        study,
        f"mechababs campaign init {label} "
        f"(apps: {', '.join(name for _, name, _ in apps)}; cluster: {cluster_file})",
        campaign,
    )
    return campaign
