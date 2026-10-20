#!/bin/sh
# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0
# Build r3dtool against an R3D SDK that lives OUTSIDE this repository.
#
#   R3DSDK_ROOT=/path/to/R3DSDKv9_2_1 native/r3dtool/build.sh
#
# Output goes to native/build/, which is git-ignored. The SDK dynamic libraries
# are copied beside the binary, unmodified, because the SDK only initialises
# when it finds them there. Do not commit anything from native/build/ or from
# the SDK: its licence forbids distributing headers, libraries and samples.
set -eu

: "${R3DSDK_ROOT:?set R3DSDK_ROOT to the unpacked R3D SDK folder}"
here=$(cd "$(dirname "$0")" && pwd)
out="$here/../build"
mkdir -p "$out"

case "$(uname -s)" in
Darwin)
	lib="$R3DSDK_ROOT/Lib/mac64/libR3DSDK-libcpp.a"
	redist="$R3DSDK_ROOT/Redistributable/mac"
	extra="-framework CoreFoundation -framework Foundation"
	;;
Linux)
	lib="$R3DSDK_ROOT/Lib/linux64/libR3DSDK-cpp11.a"
	redist="$R3DSDK_ROOT/Redistributable/linux"
	extra="-ldl -lpthread"
	;;
*)
	echo "unsupported platform: $(uname -s)" >&2
	exit 1
	;;
esac

# shellcheck disable=SC2086
c++ -std=c++11 -O2 -Wall -Wextra -I "$R3DSDK_ROOT/Include" "$here/r3dtool.cpp" "$lib" $extra -o "$out/r3dtool"
cp -f "$redist"/* "$out/"
"$out/r3dtool" version
