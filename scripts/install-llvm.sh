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

if [ -x "${BIN}/mlir-opt" ]; then
    echo "LLVM/MLIR ${VERSION} already installed at ${BIN}"
    "${BIN}/mlir-opt" --version | head -1
    exit 0
fi

echo ">> Adding apt.llvm.org repository (${CODENAME}, LLVM ${VERSION})"
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://apt.llvm.org/llvm-snapshot.gpg.key | sudo tee "${KEYRING}" >/dev/null
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
