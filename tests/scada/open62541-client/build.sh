#!/usr/bin/env bash
# Build the open62541 scenario client for the L1 SCADA suite.
#
#   tests/scada/open62541-client/build.sh        # -> tests/scada/open62541-client/build/scada_l1_open62541
#
# open62541 (MPL-2.0, test-only) is fetched at a pinned tag, the commit is checked, and it is
# built with OpenSSL encryption into $OPEN62541_PREFIX (default .deps/, git-ignored; cached in CI).
# Needs git, cmake, a C compiler and the OpenSSL headers (libssl-dev).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="v1.4.22"
COMMIT="a9d7e2f230a78d6a463cda1e66ac32d8a0661c32"
PREFIX="${OPEN62541_PREFIX:-$HERE/.deps/open62541-$VERSION}"
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"

if [ ! -f "$PREFIX/lib/cmake/open62541/open62541Config.cmake" ]; then
    SRC="$(mktemp -d)"
    trap 'rm -rf "$SRC"' EXIT
    git clone --quiet --depth 1 --branch "$VERSION" https://github.com/open62541/open62541 "$SRC/open62541"
    got="$(git -C "$SRC/open62541" rev-parse HEAD)"
    if [ "$got" != "$COMMIT" ]; then
        echo "open62541 $VERSION is at $got, expected $COMMIT: refusing to build" >&2
        exit 1
    fi
    cmake -S "$SRC/open62541" -B "$SRC/build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$PREFIX" \
        -DBUILD_SHARED_LIBS=OFF -DUA_ENABLE_ENCRYPTION=OPENSSL -DUA_ENABLE_DISCOVERY_MULTICAST=OFF \
        -DUA_BUILD_EXAMPLES=OFF -DUA_BUILD_UNIT_TESTS=OFF -DUA_FORCE_WERROR=OFF > "$SRC/cmake.log"
    cmake --build "$SRC/build" -j "$JOBS" > "$SRC/build.log" || { tail -50 "$SRC/build.log" >&2; exit 1; }
    cmake --install "$SRC/build" > /dev/null
fi

cmake -S "$HERE" -B "$HERE/build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$PREFIX" > /dev/null
cmake --build "$HERE/build" -j "$JOBS"
echo "built $HERE/build/scada_l1_open62541 (open62541 $VERSION)"
