#!/usr/bin/env bash
# Build the XLS-0096 Confidential MPT crypto add-on into the workload venv.
# xrpl-py excludes proof generation (xrpl/ext/confidential) from its core wheel, so
# fetch it from the branch and compile its cffi extension in place. The pin the
# bindings target must equal the mpt-crypto version rippled was built against — a
# mismatch would have rippled reject every proof. That rippled version is passed in
# ($2), resolved by the caller from the ACTUAL xrpld repo+ref used for the run: the
# xrpld repo may be private (raw.githubusercontent can't read it) and the ref is not
# always develop, so we no longer fetch a hardcoded public conanfile here. On
# divergence (or when the version is unknown) we do NOT fail the build (that aborts
# the whole run); we skip the crypto build so confidential valid paths go dark
# (CRYPTO_AVAILABLE=False) and drop a marker the workload reads to fire
# workload::confidential_crypto_version_mismatch. start_experiment Slack-pings it too.
set -euo pipefail

XRPL_PY_REF="${1:?usage: $0 <xrpl-py-ref> <rippled-mpt-crypto-version>}"
RIPPLED_VERSION="${2:-}"  # empty when built outside the experiment CI -> skip crypto
PYTHON="${PYTHON:-python}"

marker() {
    local m
    m="$("$PYTHON" -c 'import sys; print(sys.prefix)')/.mpt_crypto_version_mismatch"
    printf '%s\n' "$1" >"$m"
}

clone="$(mktemp -d)"
natives="$(mktemp -d)"
trap 'rm -rf "$clone" "$natives"' EXIT

git clone --depth 1 --branch "$XRPL_PY_REF" \
    https://github.com/XRPLF/xrpl-py.git "$clone"
conf_src="$clone/xrpl/ext/confidential"

# ── Version: the pin the bindings target, cross-checked against rippled ──
version="$(sed -n 's/^MPT_CRYPTO_VERSION=//p' "$conf_src/MPT_CRYPTO_VERSION")"
[ -n "$version" ] || { echo "FAIL: no MPT_CRYPTO_VERSION in xrpl-py $XRPL_PY_REF" >&2; exit 1; }

if [ -z "$RIPPLED_VERSION" ]; then
    echo "WARN: no rippled mpt-crypto version supplied (built outside experiment CI);" \
         "skipping confidential crypto build (CRYPTO_AVAILABLE=False)." >&2
    marker "xrpl-py=$version rippled=unknown"
    exit 0
fi

if [ "$version" != "$RIPPLED_VERSION" ]; then
    echo "WARN: mpt-crypto divergence — xrpl-py($XRPL_PY_REF)=$version" \
         "rippled=$RIPPLED_VERSION. Skipping confidential crypto build;" \
         "confidential valid paths disabled (CRYPTO_AVAILABLE=False)." >&2
    marker "xrpl-py=$version rippled=$RIPPLED_VERSION"
    exit 0
fi
echo "mpt-crypto $version agreed (xrpl-py $XRPL_PY_REF == rippled $RIPPLED_VERSION)"

# ── Drop the ext source beside the installed core (xrpl.ext = PEP 420 namespace) ──
site="$("$PYTHON" -c 'import xrpl, os; print(os.path.dirname(os.path.dirname(xrpl.__file__)))')"
dst="$site/xrpl/ext/confidential"
mkdir -p "$site/xrpl/ext"
rm -rf "$dst"
cp -R "$conf_src" "$dst"

# ── Fetch the version-matched native (public release; gh CLI unavailable here) ──
curl -fSL \
    "https://github.com/XRPLF/mpt-crypto/releases/download/${version}/mpt-crypto-natives-${version}.tar.gz" \
    | tar -xz -C "$natives"
mkdir -p "$dst/libs/linux"
cp "$natives/linux-x86-64/libmpt-crypto.so" "$dst/libs/linux/"

# ── Compile the cffi extension in place ($ORIGIN/libs/linux rpath finds the .so) ──
uv pip install --python "$PYTHON" cffi setuptools
"$PYTHON" "$dst/build_mpt_crypto.py"

"$PYTHON" -c "from xrpl.ext.confidential import MPT_CRYPTO_AVAILABLE; \
assert MPT_CRYPTO_AVAILABLE, 'confidential crypto failed to build'"
echo "Confidential MPT crypto ready ($version)"
