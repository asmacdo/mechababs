#!/usr/bin/env bash
#
# bootstrap.sh — build a mechababs campaign's environment (venv + vendored
# babs/mechababs pins + datalad dataset). See usage() for options.
set -euo pipefail

BABS_DEFAULT="https://github.com/PennLINC/babs.git@main"
MECHABABS_DEFAULT="https://github.com/con/mechababs.git@main"

usage() {
    cat <<'EOF'
Usage: ./bootstrap.sh <path> [--babs URL@REF] [--mechababs URL@REF]
                             [--system-site-packages]

Build a mechababs campaign's environment:
  - build .venv (optionally reusing the system site-packages)
  - clone the pinned babs + mechababs
  - make the campaign a datalad dataset, register subdatasets

  --babs URL@REF          babs pin (default: PennLINC/babs main)
  --mechababs URL@REF     mechababs pin (default: con/mechababs main)
  --system-site-packages  build the venv with access to the ambient Python's
                          installed packages, reusing a pre-built heavy stack
                          instead of rebuilding it (the e2e fixture uses this on
                          the slurm-docker-ci container, whose 2015-era compiler
                          can't build the newest wheels from source).
EOF
}

run() {
    printf '+ %s\n' "$*" >&2
    "$@"
}

save_pin() {
    # -d "$CAMPAIGN" is load-bearing: without it datalad saves inside the nested
    # clone (a no-op) instead of registering it as a campaign subdataset.
    local name="$1" ref="$2" head
    head="$(git -C "code/$name" rev-parse --short HEAD)"
    run "$VENV_DATALAD" save -d "$CAMPAIGN" -m "Vendor $name at $ref ($head)" "code/$name"
}

require_url_ref() {
    case "$1" in
        *@*) : ;;
        *) echo "expected URL@REF, got: $1" >&2; exit 1 ;;
    esac
}

CAMPAIGN=""
BABS_SPEC="$BABS_DEFAULT"
MECHABABS_SPEC="$MECHABABS_DEFAULT"
SSP=""
while [ $# -gt 0 ]; do
    case "$1" in
        --babs) BABS_SPEC="$2"; shift 2 ;;
        --mechababs) MECHABABS_SPEC="$2"; shift 2 ;;
        --system-site-packages) SSP=1; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "unknown option: $1" >&2; exit 1 ;;
        *)
            [ -n "$CAMPAIGN" ] && { echo "unexpected extra argument: $1" >&2; exit 1; }
            CAMPAIGN="$1"; shift ;;
    esac
done

[ -n "$CAMPAIGN" ] || { echo "missing <path> for the campaign" >&2; exit 1; }
require_url_ref "$BABS_SPEC"
BABS_URL="${BABS_SPEC%@*}";       BABS_REF="${BABS_SPEC##*@}"
require_url_ref "$MECHABABS_SPEC"
MECHA_URL="${MECHABABS_SPEC%@*}"; MECHA_REF="${MECHABABS_SPEC##*@}"

for tool in git uv git-annex; do
    command -v "$tool" >/dev/null 2>&1 || { echo "required tool not on PATH: $tool" >&2; exit 1; }
done
[ -e "$CAMPAIGN" ] && { echo "path already exists (fresh campaign only): $CAMPAIGN" >&2; exit 1; }

run mkdir -p "$CAMPAIGN"
CAMPAIGN="$(cd "$CAMPAIGN" && pwd)"
cd "$CAMPAIGN"

VENV="$CAMPAIGN/.venv"
VENV_PY="$VENV/bin/python"
VENV_DATALAD="$VENV/bin/datalad"

run git clone --branch "$MECHA_REF" "$MECHA_URL" code/mechababs
run git clone --branch "$BABS_REF"  "$BABS_URL"  code/babs

# venv + the mechababs pin, whose deps bring datalad (needed for `datalad create`
# below). babs is installed after registration — it is not a dep of mechababs,
# just a CLI it shells out to, and the campaign pins its version independently.
# --system-site-packages (opt-in) lets the venv reuse an ambient pre-built stack.
run uv venv ${SSP:+--system-site-packages} "$VENV"
run uv pip install --python "$VENV_PY" -e code/mechababs

# gitignore .venv before create/save so it's never captured. The ledger lock is
# added by `mechababs configure`, which owns the ledger filename constant.
printf '%s\n' '.venv/' > .gitignore

# make the campaign a datalad dataset over the populated dir, then register pins
run "$VENV_DATALAD" create --force -c text2git .
run "$VENV_DATALAD" save -d "$CAMPAIGN" -m "Ignore .venv" .gitignore
save_pin mechababs "$MECHA_REF"
save_pin babs "$BABS_REF"

# The campaign owns its cluster/pipeline configs (not the vendored tool), so the
# config that produced a run is committed in the campaign and reproduces from it
# alone. Create the dirs now, versioned from the start; git can't track an empty
# dir, so seed each with a README pointing at the starter set.
run mkdir -p clusters pipelines
cat > clusters/README.md <<'EOF'
# Campaign cluster configs

Cluster profiles for this campaign live here, so the config that produced a run is
committed in the campaign. `mechababs configure --cluster <path>` copies the config
in and resolves it by name; a `<name>` already here resolves in place. Starter
profiles to copy from (or point `--cluster` straight at) live in
`code/mechababs/examples/clusters/`.
EOF
cat > pipelines/README.md <<'EOF'
# Campaign pipeline configs

Pipeline configs for this campaign live here, so the config that produced a run is
committed in the campaign. `mechababs configure --pipelines <path>,...` copies each
config in and resolves it by name; a `<name>` already here resolves in place.
Starters live in `code/mechababs/examples/pipelines/`.
EOF
run "$VENV_DATALAD" save -d "$CAMPAIGN" -m "Add campaign clusters/ + pipelines/ config dirs" clusters pipelines

# editable-install the pinned babs + the campaign extras into the venv.
# Under --system-site-packages, install babs WITHOUT its deps: they come from the
# ambient env (--system-site-packages made them importable), and reinstalling
# them into the venv would rebuild babs's heavy stack from source — which fails on
# a host whose compiler can't build the newest wheels (the e2e slurm-docker-ci
# container). Trusts the ambient env to satisfy babs's deps.
run uv pip install --python "$VENV_PY" ${SSP:+--no-deps} -e code/babs
if [ -f code/mechababs/requirements-campaign.txt ]; then
    run uv pip install --python "$VENV_PY" -r code/mechababs/requirements-campaign.txt
fi

echo >&2
echo "Environment ready at $CAMPAIGN" >&2
echo "Next, from the campaign venv, configure the campaign:" >&2
echo "  source .venv/bin/activate" >&2
echo "  mechababs configure --pipelines <a.yaml,...> --cluster <c.yaml>" >&2
