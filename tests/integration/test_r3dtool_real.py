# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Integration: the real r3dtool against the real R3D SDK and, optionally, a real clip.

Skipped unless native/build/r3dtool has been built (see native/r3dtool/build.sh). Set DAILIES_TEST_R3D
to the first file of a real clip to exercise metadata too. No SDK file and no camera media is ever
stored in this repository.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from dailies.commands.base import Command, CommandLog, SubprocessExecutor
from dailies.decoder import SdkDecoder

TOOL = Path(__file__).resolve().parents[2] / "native" / "build" / "r3dtool"
CLIP = os.environ.get("DAILIES_TEST_R3D", "")
pytestmark = pytest.mark.skipif(not TOOL.is_file(), reason="r3dtool is not built, run native/r3dtool/build.sh")


class _Config:
    class tools:
        class r3dtool:
            path = TOOL


def run(*args: str):
    return SubprocessExecutor(CommandLog(io.StringIO())).run(Command((str(TOOL), *args)), clip="-", stage="scan")


def test_the_sdk_initialises_from_beside_the_binary():
    result = run("version")
    assert result.ok, result.stderr
    assert SdkDecoder(_Config).parse_version(result.stdout).startswith("0.1.0 / R3DAPI ")


def test_usage_error_is_exit_2():
    assert run("frobnicate").returncode == 2


def test_a_file_that_is_not_a_clip_is_exit_4(tmp_path):
    bogus = tmp_path / "A001_C001_0101AA_001.R3D"
    bogus.write_bytes(b"not a clip")
    result = run("meta", str(bogus))
    assert result.returncode == 4 and "failed to load clip" in result.stderr


@pytest.mark.skipif(not CLIP, reason="set DAILIES_TEST_R3D to a real clip")
def test_real_clip_metadata_parses():
    result = run("meta", CLIP)
    assert result.ok, result.stderr
    facts = SdkDecoder(_Config).parse_metadata(result.stdout)
    assert facts.frames > 0 and facts.width > 0 and Path(CLIP).name.startswith(facts.clip_name)
