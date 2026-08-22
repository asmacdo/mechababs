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
exact versions (a commit, for a git source) and writes them into ``uv.lock``, which
is committed to the study. That file — not a vendored code clone — is what says which
tools ran, and a mid-campaign version bump is an edit to it, with its git history as
the record of the campaign's config epochs. The mechababs pin is read from the running
install (PEP 610 ``direct_url.json``), so the campaign records the mechababs the user
actually invoked rather than a ref they would have to retype; babs, which mechababs
only shells out to, defaults to its latest **release** — declared as a plain
dependency and frozen to an exact version by the lock — and ``--babs URL@REF`` pins a
git checkout instead, which is how a PR branch (or a local one) gets run through a
whole campaign.
"""

import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from importlib import metadata
from pathlib import Path

import yaml

from mechababs import campaign as campaign_mod
from mechababs.utils import campaign_save_scope

# Runtime tools a campaign needs beyond mechababs + babs themselves. A literal rather
# than a requirements file in the repo, because this command may run from an ephemeral
# uvx install, which has the mechababs *package* but no repo file to read.
CAMPAIGN_EXTRAS = [
    "con-duct",     # usage/resource logs alongside every run
    "visidata",     # interactive TSV viewer for the statefile
    "pytest",       # runs the packaged e2e scenario behind `mechababs test-cluster`
]

# A label names a directory and is exported as an env var, so keep it boring.
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Everything mechababs writes under a campaign dir is small text that a clone must
# be able to read *without* fetching annex content — the lock above all, since
# rebuilding the environment from a fresh clone is the whole reproduction story.
# So the routing is declared once, as an attribute on the campaign dir, instead of
# being asked for per save: it then holds for every later writer into the campaign,
# and no save has to carry a `to_git` flag that would mis-route the moment its scope
# reached a real subdataset.
GITATTRIBUTES = "* annex.largefiles=nothing\n"

UV = "uv"


# --------------------------------------------------------------------------
# Pinning the tools
# --------------------------------------------------------------------------

def parse_source_spec(spec, what):
    """Split a ``URL@REF`` pin. ``URL`` is anything git clones, a local path included.

    ``REF`` is required rather than defaulted: naming it explicitly is what makes
    "run a campaign against this PR branch" a config change instead of a code change.
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
    except metadata.PackageNotFoundError:
        # Running from a bare checkout (PYTHONPATH, no install): there is no
        # install metadata to read, and an unsourced "mechababs" requirement
        # would send uv to PyPI, where mechababs does not exist — a confusing
        # resolver error far from the cause. Fail here, naming the fix.
        sys.exit("cannot detect the running mechababs install (no distribution "
                 "metadata) — pass --mechababs URL@REF to pin it explicitly")
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


def cluster_env_constraints(config_path):
    """The cluster config's ``env_constraints``, or ``[]``.

    A list of verbatim PEP 508 specifiers (``pandas<=2.3.2``). Which package versions
    a site can actually install is a **cluster fact** — an old glibc stops loading the
    newest manylinux wheels long before it stops running jobs — so it is declared on
    the cluster axis and folded into the campaign's generated pyproject.

    mechababs does not interpret them: they become uv ``constraint-dependencies``,
    whose semantics (cap a package that is already in the resolution, never pull one
    in that is not) are uv's own. The only check here is shape, and it insists on a
    real list because the near-misses fail *quietly*: a bare string iterates one
    constraint per character, and a mapping iterates its keys, dropping every
    specifier — either way the resolution looks capped and is not.
    """
    config = yaml.safe_load(Path(config_path).read_text()) or {}
    constraints = config.get("env_constraints")
    if not constraints:
        return []
    if not isinstance(constraints, list) or \
            not all(isinstance(c, str) for c in constraints):
        sys.exit(f"env_constraints in {config_path} must be a LIST of version "
                 f"specifiers (e.g. `- pandas<=2.3.2`), got: {constraints!r}")
    return list(constraints)


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


def render_pyproject(label, mechababs_req, mechababs_source, babs_source=None,
                     env_constraints=()):
    """The campaign's dependency declaration — a uv *virtual* project.

    No ``[build-system]``: the campaign is not a package to build, it is a set of
    pinned dependencies for ``uv lock`` / ``uv sync`` to resolve and install.

    A tool with no ``[tool.uv.sources]`` entry is a plain dependency, resolved from
    PyPI and frozen to an exact released version by the lock — the default for both
    babs and a registry-installed mechababs. A source entry overrides that with a
    git (or path) checkout.

    ``env_constraints`` (the cluster config's) become uv ``constraint-dependencies``:
    caps that apply to whatever the resolution already contains, deep in the transitive
    closure included, without adding a dependency or pinning one the site does not use.
    """
    deps = [mechababs_req, "babs", *CAMPAIGN_EXTRAS]
    sources = {}
    if mechababs_source:
        sources["mechababs"] = mechababs_source
    if babs_source:
        sources["babs"] = babs_source

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
    lines += ["]"]
    if sources:
        lines += ["", "[tool.uv.sources]"]
        lines += [f"{name} = {_toml_inline(source)}" for name, source in sources.items()]
    if env_constraints:
        lines += [
            "",
            "# `env_constraints` from this campaign's cluster config: version caps the",
            "# SITE imposes (typically a glibc too old for the newest manylinux wheels).",
            "# Constraints cap a package only if the resolution already contains it —",
            "# they never add one — so this is a floor-preserving narrowing, not a pin.",
            "[tool.uv]",
            "constraint-dependencies = [",
        ]
        lines += [f"    {_toml_str(c)}," for c in env_constraints]
        lines += ["]"]
    return "\n".join(lines) + "\n"


# uv's own line when a source build fails, e.g. ``Failed to build `pandas==2.3.3` ``.
# It precedes the build backend's output, so it survives however many hundred lines of
# compiler error follow.
UV_BUILD_FAILURE_RE = re.compile(r"Failed to build [`'\"]([A-Za-z0-9._-]+)")


def missing_wheel_message(package, campaign, cluster_file):
    """What to tell a user whose site cannot install ``package``."""
    return (
        f"\ncould not build the campaign environment: uv had no installable wheel for "
        f"{package!r} on this system and building it from source failed (above).\n"
        f"\nThat is a SITE fact, and `env_constraints` in the cluster config is where "
        f"it is declared — most often a glibc older than the newest manylinux wheels "
        f"target. Cap the package to a version that still ships a wheel here:\n"
        f"\n    # {cluster_file}\n"
        f"    env_constraints:\n"
        f"      - {package}<=<the last version with a wheel for this system>\n"
        f"\nThen remove the half-built campaign and run `mechababs campaign init` "
        f"again — init does not re-run over an existing campaign:\n"
        f"\n    rm -rf {campaign}\n"
    )


def run_uv(*args, campaign, cluster_file):
    """Run a ``uv`` command, and translate a source-build failure into a named one.

    A package with no wheel for this system does not announce itself as one: uv falls
    back to the sdist, and what reaches the user is the build backend's compiler error
    — hundreds of lines naming a missing header rather than a package, with the actual
    lever (`env_constraints`) nowhere in sight.

    uv's blanket ``no-build`` would turn that into a clean "no wheel for X", but it
    covers *every* source distribution, and a campaign's mechababs (and often babs) is
    pinned to a git or path source, which is one — so it fails every campaign on every
    platform. Scoping it per-package needs an allowlist uv does not have. So the output
    is streamed (a resolve is slow; silence would be worse) and kept, and uv's own
    ``Failed to build `<name>` `` line is what names the package afterwards.
    """
    cmd = [UV, *[str(a) for a in args]]
    print("+ " + " ".join(cmd), file=sys.stderr)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    captured = []
    for line in proc.stdout:
        sys.stderr.write(line)
        captured.append(line)
    if proc.wait() == 0:
        return
    failed = UV_BUILD_FAILURE_RE.findall("".join(captured))
    if not failed:
        # Not a build failure at all (an unreachable pin, no network, a bad
        # specifier). Say so plainly rather than dressing it as a platform problem.
        sys.exit(f"\n{' '.join(cmd)} failed (exit {proc.returncode})")
    sys.exit(missing_wheel_message(failed[0], campaign, cluster_file))


def build_env(campaign, label, cluster_file):
    """Resolve the campaign's lock and build its venv from it; stamp the venv.

    ``uv lock`` pins every dependency (the git refs to commits) and ``uv sync``
    installs exactly that — so the environment and the committed lock agree by
    construction, which is what the env-match guard later checks.

    ``cluster_file`` names the config a failure should send the user to edit; both uv
    steps can hit a source build (lock builds an sdist it cannot read metadata from,
    sync builds one that has no wheel), so both go through ``run_uv``.
    """
    uv = dict(campaign=campaign, cluster_file=cluster_file)
    run_uv("lock", "--project", str(campaign), **uv)
    run_uv("sync", "--project", str(campaign), "--frozen", **uv)
    venv = campaign / campaign_mod.VENV_DIRNAME
    campaign_mod.write_env_stamp(
        venv, label, (campaign / campaign_mod.UV_LOCK_FILENAME).read_text())
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
         babs_spec=None, mechababs_spec=None):
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

    # Everything that writes runs inside the scope: it checks the target is clean
    # first, and commits what the block produced as one attributable node.
    with campaign_save_scope(study, campaign) as save:
        campaign.mkdir(parents=True)

        # First file in, before anything it has to govern: git-annex reads the
        # working tree's attributes as it adds, so the attribute must never be
        # younger than a save that could reach these paths.
        (campaign / ".gitattributes").write_text(GITATTRIBUTES)

        # The venv is ephemeral and rebuilt from the lock; the flock is a runtime
        # artifact. Ignore both from INSIDE the campaign dir, so mechababs' whole
        # footprint stays under .mechababs/ and the study's own .gitignore is left
        # alone. Untracked-but-not-ignored files here would dirty the study, which
        # the clean-in guards read as unattributable work.
        (campaign / ".gitignore").write_text(
            f"{campaign_mod.VENV_DIRNAME}/\n{campaign_mod.FLOCK_FILENAME}\n")

        apps = resolve_apps(campaign / campaign_mod.APPS_DIRNAME, app_args)
        cluster_file = stage_config(
            campaign / campaign_mod.CLUSTERS_DIRNAME, cluster_arg, "cluster")

        config = {
            "label": label,
            "apps": [f"{campaign_mod.APPS_DIRNAME}/{filename}"
                     for filename, _, _ in apps],
            "cluster": f"{campaign_mod.CLUSTERS_DIRNAME}/{cluster_file}",
            "limit": limit,
        }
        (campaign / campaign_mod.CONFIG_FILENAME).write_text(
            yaml.safe_dump(config, sort_keys=False))

        # Header only: which source datasets a campaign acts on is an explicit
        # selection, made by `add-dataset`, not implied by init.
        (campaign / campaign_mod.STATE_FILENAME).write_text(
            campaign_mod.initial_header())

        if mechababs_spec:
            mechababs_req, mechababs_source = "mechababs", git_source(
                *parse_source_spec(mechababs_spec, "mechababs"))
        else:
            mechababs_req, mechababs_source = running_mechababs_pin()
        # No --babs: babs stays a plain dependency, so uv resolves the latest release
        # from PyPI and freezes that version in the lock. A git ref is the override,
        # for running a PR branch (or a local one) through a campaign.
        babs_source = (git_source(*parse_source_spec(babs_spec, "babs"))
                       if babs_spec else None)
        # The STAGED copy, not the argument: that is the file committed with the
        # campaign, so it is what a failure should tell the user to edit — and the same
        # read whether the config arrived as a path or a URL.
        staged_cluster = campaign_mod.clusters_dir(study, label) / cluster_file
        (campaign / campaign_mod.PYPROJECT_FILENAME).write_text(
            render_pyproject(label, mechababs_req, mechababs_source, babs_source,
                             cluster_env_constraints(staged_cluster)))

        write_env_sh(campaign, label)
        build_env(campaign, label, staged_cluster)

        save.message = (
            f"mechababs campaign init {label} "
            f"(apps: {', '.join(name for _, name, _ in apps)}; "
            f"cluster: {cluster_file})")
    return campaign
