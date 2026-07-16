#!/usr/bin/env bash
# Build the XLS-0096 Confidential MPT crypto add-on into the workload venv.
# xrpl-py excludes proof generation (xrpl/ext/confidential) from its core wheel, so
# fetch it from the branch and compile its cffi extension in place. The mpt-crypto
# version is read from the branch (not hardcoded) and must equal rippled's conan pin
# — a mismatch would have rippled reject every proof. On divergence we do NOT fail the
# build (that aborts the whole run); we skip the crypto build so confidential valid
# paths go dark (CRYPTO_AVAILABLE=False) and drop a marker the workload reads to fire
# workload::confidential_crypto_version_mismatch. start_experiment Slack-pings it too.
set -euo pipefail

XRPL_PY_REF="${1:?usage: $0 <xrpl-py-ref> <rippled-ref>}"
RIPPLED_REF="${2:?usage: $0 <xrpl-py-ref> <rippled-ref>}"
PYTHON="${PYTHON:-python}"

clone="$(mktemp -d)"
natives="$(mktemp -d)"
trap 'rm -rf "$clone" "$natives"' EXIT

git clone --depth 1 --branch "$XRPL_PY_REF" \
    https://github.com/XRPLF/xrpl-py.git "$clone"
conf_src="$clone/xrpl/ext/confidential"

# ── Version: the pin the bindings target, cross-checked against rippled ──
version="$(sed -n 's/^MPT_CRYPTO_VERSION=//p' "$conf_src/MPT_CRYPTO_VERSION")"
[ -n "$version" ] || { echo "FAIL: no MPT_CRYPTO_VERSION in xrpl-py $XRPL_PY_REF" >&2; exit 1; }

rippled_version="$(curl -fsSL \
    "https://raw.githubusercontent.com/XRPLF/rippled/${RIPPLED_REF}/conanfile.py" \
    | grep -oE 'mpt-crypto/[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?' | head -1 | cut -d/ -f2)"
[ -n "$rippled_version" ] || {
    echo "FAIL: no mpt-crypto pin in rippled ${RIPPLED_REF} conanfile.py" >&2; exit 1; }

if [ "$version" != "$rippled_version" ]; then
    echo "WARN: mpt-crypto divergence — xrpl-py($XRPL_PY_REF)=$version" \
         "rippled($RIPPLED_REF)=$rippled_version. Skipping confidential crypto build;" \
         "confidential valid paths disabled (CRYPTO_AVAILABLE=False)." >&2
    marker="$("$PYTHON" -c 'import sys; print(sys.prefix)')/.mpt_crypto_version_mismatch"
    printf 'xrpl-py=%s rippled=%s\n' "$version" "$rippled_version" >"$marker"
    exit 0
fi
echo "mpt-crypto $version agreed (xrpl-py $XRPL_PY_REF == rippled $RIPPLED_REF)"

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
