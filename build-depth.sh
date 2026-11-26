#!/bin/sh
set -eu

cd "$(dirname "$0")"
prefix="${FREENECT_PREFIX:-$HOME/opt}"
clang -std=c99 -O2 -Wall -Wextra -Werror depth_stream.c -o depth_stream \
    -I"$prefix/include" -L"$prefix/lib" -lfreenect -Wl,-rpath,"$prefix/lib"
