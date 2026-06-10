#!/usr/bin/env bash
# Report the LLVM/MLIR toolchain resolved on the current PATH.
set -euo pipefail

echo "Resolved toolchain (PATH order):"
for t in clang llc opt lli mlir-opt mlir-translate lld llvm-config; do
    if path="$(command -v "$t" 2>/dev/null)"; then
        printf "  %-15s %s\n" "$t" "$path"
    else
        printf "  %-15s MISSING\n" "$t"
    fi
done

echo
if command -v clang >/dev/null 2>&1; then
    echo -n "clang:    "; clang --version | head -1
fi
if command -v mlir-opt >/dev/null 2>&1; then
    echo -n "mlir-opt: "; mlir-opt --version | grep -i "LLVM version" | head -1 || mlir-opt --version | head -1
fi
