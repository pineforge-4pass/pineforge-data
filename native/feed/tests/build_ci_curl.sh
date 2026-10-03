#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
prefix="${1:?provide an installation prefix}"
source_dir="${prefix}-source"
mkdir -p "$source_dir"
archive="$source_dir/curl-8.14.1.tar.xz"
curl --fail --location --retry 3 https://curl.se/download/curl-8.14.1.tar.xz -o "$archive"
printf '%s  %s\n' f4619a1e2474c4bbfedc88a7c2191209c8334b48fa1f4e53fd584cc12e9120dd "$archive" | shasum -a 256 --check
tar -xJf "$archive" -C "$source_dir"
cmake -S "$source_dir/curl-8.14.1" -B "$source_dir/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" -DCURL_DISABLE_WEBSOCKETS=OFF \
  -DCURL_USE_OPENSSL=ON -DBUILD_SHARED_LIBS=ON -DBUILD_TESTING=OFF \
  -DCURL_USE_LIBPSL=OFF -DCURL_USE_LIBSSH2=OFF -DCURL_USE_LIBSSH=OFF \
  -DCURL_BROTLI=OFF -DCURL_ZSTD=OFF
cmake --build "$source_dir/build" -j4
cmake --install "$source_dir/build"
LD_LIBRARY_PATH="$prefix/lib:${LD_LIBRARY_PATH:-}" \
  DYLD_LIBRARY_PATH="$prefix/lib:${DYLD_LIBRARY_PATH:-}" "$prefix/bin/curl" --version
