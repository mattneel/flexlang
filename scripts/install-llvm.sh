#!/usr/bin/env bash
# Install the LLVM/MLIR toolchain (clang, llc, opt, lli, mlir-opt,
# mlir-translate, lld) from apt.llvm.org on Ubuntu. Idempotent.
#
#   bash scripts/install-llvm.sh          # installs FLX_LLVM_VERSION (default 22)
#   FLX_LLVM_VERSION=22 bash scripts/install-llvm.sh
set -euo pipefail

VERSION="${FLX_LLVM_VERSION:-22}"
CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME}")"
KEYRING="/etc/apt/keyrings/apt.llvm.org.asc"
LIST="/etc/apt/sources.list.d/llvm-${VERSION}.list"
BIN="/usr/lib/llvm-${VERSION}/bin"
EXPECTED_LLVM_KEY_FINGERPRINT="6084 F3CF 814B 57C1 CF12  EFD5 15CF 4D18 AF4F 7421"

if [ -x "${BIN}/mlir-opt" ]; then
    echo "LLVM/MLIR ${VERSION} already installed at ${BIN}"
    "${BIN}/mlir-opt" --version | head -1
    exit 0
fi

echo ">> Adding apt.llvm.org repository (${CODENAME}, LLVM ${VERSION})"
sudo install -d -m 0755 /etc/apt/keyrings
tmp_key="$(mktemp)"
trap 'rm -f "${tmp_key}"' EXIT
curl -fsSL https://apt.llvm.org/llvm-snapshot.gpg.key -o "${tmp_key}"
actual_fingerprint="$(
    gpg --show-keys --with-fingerprint "${tmp_key}" 2>/dev/null \
        | awk '/^[[:space:]]*[0-9A-F]{4}/ { gsub(/[[:space:]]/, "", $0); print; exit }'
)"
expected_fingerprint="${EXPECTED_LLVM_KEY_FINGERPRINT//[[:space:]]/}"
if [ "${actual_fingerprint}" != "${expected_fingerprint}" ]; then
    echo "LLVM apt key fingerprint mismatch" >&2
    echo "  expected: ${EXPECTED_LLVM_KEY_FINGERPRINT}" >&2
    echo "  actual:   ${actual_fingerprint:-<none>}" >&2
    exit 1
fi
sudo tee "${KEYRING}" <"${tmp_key}" >/dev/null
echo "deb [signed-by=${KEYRING}] http://apt.llvm.org/${CODENAME}/ llvm-toolchain-${CODENAME}-${VERSION} main" \
    | sudo tee "${LIST}" >/dev/null

echo ">> Installing packages"
sudo apt-get update
sudo apt-get install -y \
    "llvm-${VERSION}" \
    "clang-${VERSION}" \
    "mlir-${VERSION}-tools" \
    "lld-${VERSION}"

echo ">> Installed:"
"${BIN}/mlir-opt" --version | head -1
echo "Toolchain is at ${BIN} (mise prepends it to PATH for this repo)."
