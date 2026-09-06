#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compiler=${CXX:-x86_64-w64-mingw32-g++}
mkdir -p "$root/runtime/linux" "$root/runtime/caller"
"$compiler" -std=c++17 -O2 -static -municode "$root/native/frame_worker.cpp" \
    -o "$root/runtime/linux/dlss5-worker.exe" -ld3d12 -ldxgi -ldxguid -lole32
"$compiler" -std=c++17 -O2 -static -shared "$root/native/caller_shim.cpp" \
    -o "$root/runtime/caller/nvngx.dll_comfy.dll"
