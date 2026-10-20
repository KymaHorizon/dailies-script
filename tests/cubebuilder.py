# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Write .cube files from a Python function. Test-only code."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

Rgb = tuple[float, float, float]


def write_cube(path: Path, size: int, function: Callable[[Rgb], Rgb], title: str = "test") -> Path:
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {size}", ""]
    for b in range(size):
        for g in range(size):
            for r in range(size):  # red changes fastest, as the format requires
                out = function((r / (size - 1), g / (size - 1), b / (size - 1)))
                lines.append(" ".join(f"{v:.10f}" for v in out))
    path.write_text("\n".join(lines) + "\n")
    return path


def identity(rgb: Rgb) -> Rgb:
    return rgb


def log_to_display(rgb: Rgb) -> Rgb:
    """A smooth, strongly non-linear curve with channel crosstalk, standing in for a show LUT."""
    r, g, b = (v ** 2.2 for v in rgb)
    return (0.9 * r + 0.1 * g, 0.05 * r + 0.9 * g + 0.05 * b, 0.1 * g + 0.9 * b)
