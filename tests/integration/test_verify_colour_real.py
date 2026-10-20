# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Integration: the reference frame check through a real FFmpeg, with a real baked cube and the exact chain.

The "colourist's reference" is produced here by OpenColorIO, pixel by pixel, with no FFmpeg involved, so this
is an independent check that the render's colour chain and OCIO agree on real pixels."""

from __future__ import annotations

import array
import io
import random
import struct
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("PyOpenColorIO")

from cubebuilder import log_to_display, write_cube
from dailies.commands.base import CommandLog, SubprocessExecutor
from dailies.commands.verify import build_verify_command, parse_psnr
from dailies.grade.bake import Recipe, _processor, bake
from dailies.grade.cdl import Cdl

FFMPEG = next((Path(p) for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg") if Path(p).is_file()), None)
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="needs FFmpeg")
W, H = 96, 54
GRADE = Cdl((1.12, 1.0, 0.93), (0.02, 0.0, -0.015), (1.04, 1.0, 0.97), 0.9, "14A")


class _Config:
    class tools:
        class ffmpeg:
            path = FFMPEG


def tiff16(path: Path, pixels: list[tuple[float, float, float]]) -> Path:
    raw = path.with_suffix(".raw")
    raw.write_bytes(b"".join(struct.pack("<3H", *(min(max(round(c * 65535), 0), 65535) for c in p)) for p in pixels))
    subprocess.run([str(FFMPEG), "-v", "error", "-y", "-f", "rawvideo", "-pixel_format", "rgb48le", "-video_size", f"{W}x{H}",
                    "-i", str(raw), "-frames:v", "1", "-pix_fmt", "rgb48le", str(path)], check=True)
    return path


@pytest.fixture(scope="module")
def frames(tmp_path_factory):
    root = tmp_path_factory.mktemp("verify")
    show = write_cube(root / "show.cube", 33, log_to_display)
    random.seed(5)
    pixels = []
    for _ in range(W * H):
        v = random.uniform(0.05, 0.75)
        pixels.append(tuple(min(max(v + random.uniform(-0.06, 0.06), 0.0), 1.0) for _ in range(3)))
    pixels = [tuple(round(c * 65535) / 65535 for c in p) for p in pixels]
    recipe = Recipe(GRADE, None, show, None, "x", root / "unused.ocio", 65, False)
    buffer = array.array("f", [c for p in pixels for c in p])
    _processor(recipe).applyRGB(buffer)
    graded = [tuple(buffer[i:i + 3]) for i in range(0, len(buffer), 3)]
    return {"root": root, "show": show, "recipe": recipe, "source": tiff16(root / "source.tif", pixels),
            "reference": tiff16(root / "reference.tif", graded),
            "wrong": tiff16(root / "wrong.tif", [(min(r * 1.08, 1.0), g, b) for r, g, b in graded])}


def psnr(colour: dict, frames: dict, reference: str) -> float:
    command = build_verify_command(_Config, colour, frames["source"], frames[reference])
    result = SubprocessExecutor(CommandLog(io.StringIO())).run(command, clip="c", stage="verify-colour")
    assert result.ok, result.stderr[-800:]
    return parse_psnr(result.stderr)


def test_exact_chain_matches_opencolorio_on_real_pixels(frames):
    colour = {"apply": "exact", "lut_hash": "x", "show_lut": str(frames["show"]), "camera_look": None, "cdl": GRADE.to_dict()}
    assert psnr(colour, frames, "reference") > 75


def test_baked_cube_is_close_but_measurably_worse_than_the_exact_chain(frames):
    exact = {"apply": "exact", "lut_hash": "x", "show_lut": str(frames["show"]), "camera_look": None, "cdl": GRADE.to_dict()}
    baked = {"apply": "baked", "lut_hash": "x", "baked_lut": str(bake(frames["recipe"], frames["root"]).path)}
    assert 45 < psnr(baked, frames, "reference") < psnr(exact, frames, "reference")


def test_a_reference_graded_differently_is_caught(frames):
    colour = {"apply": "exact", "lut_hash": "x", "show_lut": str(frames["show"]), "camera_look": None, "cdl": GRADE.to_dict()}
    assert psnr(colour, frames, "wrong") < 40
