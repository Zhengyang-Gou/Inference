#!/bin/bash
# Build script for simccl-standalone
# Usage: ./build.sh [v2.20|v2.30]
set -e
MOCK_VERSION="${1:-v2.30}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"

echo "[build.sh] Building simccl-standalone with mock version: $MOCK_VERSION"
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
cmake .. -DMOCK_VERSION="$MOCK_VERSION" -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)
echo "[build.sh] Done: $BUILD_DIR/simccl-standalone"
