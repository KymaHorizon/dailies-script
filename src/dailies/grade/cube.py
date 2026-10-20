# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Read and sample 3D .cube files.

Used to check a baked cube against an independent calculation, and to measure
how much a given cube size loses. Sampling is tetrahedral, the same method the
render stage asks FFmpeg for, so numbers measured here describe what the render
will actually do.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

Rgb = tuple[float, float, float]


class CubeError(Exception):
    """This .cube file cannot be used."""


@dataclass(frozen=True)
class Cube:
    size: int
    values: tuple[Rgb, ...]  # red changes fastest, then green, then blue

    def at(self, r: int, g: int, b: int) -> Rgb:
        return self.values[r + self.size * (g + self.size * b)]


def read_cube(path: Path) -> Cube:
    try:
        text = Path(path).read_text()
    except OSError as error:
        raise CubeError(f"cannot read {path}: {error}") from None
    size = None
    values: list[Rgb] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keyword = line.split()[0]
        if keyword == "LUT_1D_SIZE":
            raise CubeError(f"{Path(path).name} is a 1D LUT, a 3D cube is required")
        if keyword == "LUT_3D_SIZE":
            size = int(line.split()[1])
            continue
        if keyword[0].isalpha():
            continue  # TITLE, DOMAIN_MIN, DOMAIN_MAX and the like
        try:
            r, g, b = (float(part) for part in line.split())
        except ValueError:
            raise CubeError(f"{Path(path).name}: expected three numbers, got {line!r}") from None
        values.append((r, g, b))
    if size is None:
        raise CubeError(f"{Path(path).name} has no LUT_3D_SIZE line")
    if len(values) != size ** 3:
        raise CubeError(f"{Path(path).name}: expected {size ** 3} entries for size {size}, found {len(values)}")
    return Cube(size, tuple(values))


def sample_cube(cube: Cube, rgb: Rgb) -> Rgb:
    """Tetrahedral interpolation. Input outside 0 to 1 is clamped, as every cube reader does."""
    n = cube.size - 1
    position = [min(max(v, 0.0), 1.0) * n for v in rgb]
    base = [min(int(p), n - 1) for p in position]
    fr, fg, fb = (p - b for p, b in zip(position, base))
    r0, g0, b0 = base

    def corner(dr: int, dg: int, db: int) -> Rgb:
        return cube.at(r0 + dr, g0 + dg, b0 + db)

    c000, c111 = corner(0, 0, 0), corner(1, 1, 1)
    if fr > fg:
        if fg > fb:
            steps = ((fr, corner(1, 0, 0), c000), (fg, corner(1, 1, 0), corner(1, 0, 0)), (fb, c111, corner(1, 1, 0)))
        elif fr > fb:
            steps = ((fr, corner(1, 0, 0), c000), (fb, corner(1, 0, 1), corner(1, 0, 0)), (fg, c111, corner(1, 0, 1)))
        else:
            steps = ((fb, corner(0, 0, 1), c000), (fr, corner(1, 0, 1), corner(0, 0, 1)), (fg, c111, corner(1, 0, 1)))
    elif fb > fg:
        steps = ((fb, corner(0, 0, 1), c000), (fg, corner(0, 1, 1), corner(0, 0, 1)), (fr, c111, corner(0, 1, 1)))
    elif fb > fr:
        steps = ((fg, corner(0, 1, 0), c000), (fb, corner(0, 1, 1), corner(0, 1, 0)), (fr, c111, corner(0, 1, 1)))
    else:
        steps = ((fg, corner(0, 1, 0), c000), (fr, corner(1, 1, 0), corner(0, 1, 0)), (fb, c111, corner(1, 1, 0)))
    out = list(c000)
    for weight, high, low in steps:
        for channel in range(3):
            out[channel] += weight * (high[channel] - low[channel])
    return (out[0], out[1], out[2])
