# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Reference frame check for the colour chain.

Takes one log frame exactly as the decoder produces it, runs it through the same colour filters the
render uses for that clip, and compares the result with the same frame graded by the colourist. Both
frames must have the same size, and the reference must be full range RGB with no resize and no burn-in.
The number reported is FFmpeg's PSNR over the whole frame. 60 dB and above is a match to within a
ten bit code or so. Below about 45 dB something in the chain differs, most often the LUT, the CDL
clamp behaviour, or a range mismatch in the exported reference.
"""

from __future__ import annotations

import re
from pathlib import Path

from dailies.commands.base import Command
from dailies.commands.render import colour_filters
from dailies.config import ShowConfig

_PSNR = re.compile(r"PSNR .*average:(inf|[0-9.]+)")


class VerifyError(Exception):
    """The comparison did not produce a result. Never treated as a pass."""


def build_verify_command(config: ShowConfig, colour: dict, source: Path, reference: Path) -> Command:
    graded = ",".join(["format=rgb48le", *colour_filters(colour), "format=rgb48le"])
    graph = f"[0:v]{graded}[graded];[1:v]format=rgb48le[reference];[graded][reference]psnr"
    return Command((str(config.tools.ffmpeg.path), "-hide_banner", "-nostdin", "-i", str(source), "-i", str(reference),
                    "-filter_complex", graph, "-f", "null", "-"))


def parse_psnr(stderr: str) -> float:
    match = _PSNR.search(stderr)
    if not match:
        raise VerifyError("FFmpeg printed no PSNR summary, the frames were probably not compared")
    return float(match.group(1))
