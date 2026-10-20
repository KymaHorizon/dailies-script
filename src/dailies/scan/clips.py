# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""R3D discovery.

A RED clip that exceeds the file size limit spans several files: _001, _002
and so on. They are one logical clip. Only the first file is ever addressed,
because the decoder follows the span itself. Verified with the R3D SDK on real
V-RAPTOR media: addressing _002 directly still loads the whole clip, so
treating parts separately would scan the same clip twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# R3D parts always carry a span number. A ProRes movie from a RED normally does too, but a movie
# without one is accepted as a single part clip.
_PART = re.compile(r"^(?P<name>[^.].*?)(?:_(?P<part>\d{3}))?\.(?P<ext>r3d|mov)$", re.IGNORECASE)


@dataclass(frozen=True)
class SourceClip:
    clip_name: str
    first: Path  # the file to hand to the decoder
    parts: tuple[Path, ...]
    size_bytes: int
    gaps: tuple[int, ...]  # span numbers that should exist and do not
    kind: str = "r3d"  # "r3d" or "mov"


def discover_clips(root: Path) -> list[SourceClip]:
    grouped: dict[tuple[Path, str, str], dict[int, Path]] = {}
    for path in Path(root).rglob("*"):
        match = _PART.match(path.name)
        if not match or not path.is_file():
            continue
        kind = match["ext"].lower()
        if match["part"] is None and kind == "r3d":
            continue  # an R3D without a span number is not a camera original
        grouped.setdefault((path.parent, match["name"], kind), {})[int(match["part"] or 1)] = path
    clips = []
    for (_, name, kind), parts in grouped.items():
        numbers = sorted(parts)
        ordered = tuple(parts[n] for n in numbers)
        gaps = tuple(n for n in range(1, numbers[-1] + 1) if n not in parts)
        clips.append(SourceClip(name, ordered[0], ordered, sum(p.stat().st_size for p in ordered), gaps, kind))
    return sorted(clips, key=lambda clip: (clip.clip_name, clip.kind, str(clip.first)))
