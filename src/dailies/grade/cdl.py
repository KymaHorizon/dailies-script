# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""ASC CDL files.

Three file types are in use on real shows: .cdl (ColorDecisionList), .ccc
(ColorCorrectionCollection) and .cc (a bare ColorCorrection). The saturation
node is spelled SATNode by some tools and SatNode by others, and the namespace
version varies or is absent. All of that is accepted. Values are not: a slope
or saturation below zero, a power that is not above zero, a missing SOP value
or anything that is not a finite number is an error, because a colour decision
that cannot be read exactly must never be guessed at.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

Triple = tuple[float, float, float]


class CdlError(Exception):
    """This CDL file cannot be used."""


@dataclass(frozen=True)
class Cdl:
    slope: Triple
    offset: Triple
    power: Triple
    sat: float
    id: str

    @property
    def is_identity(self) -> bool:
        return (self.slope, self.offset, self.power, self.sat) == ((1, 1, 1), (0, 0, 0), (1, 1, 1), 1)

    def to_dict(self) -> dict:
        return {"slope": list(self.slope), "offset": list(self.offset), "power": list(self.power), "sat": self.sat}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(node: ET.Element, *names: str) -> ET.Element | None:
    wanted = {name.lower() for name in names}
    return next((child for child in node.iter() if _local(child.tag).lower() in wanted), None)


def _triple(sop: ET.Element | None, name: str, *, minimum: float, exclusive: bool) -> Triple:
    node = _find(sop, name) if sop is not None else None
    try:
        values = tuple(float(part) for part in (node.text or "").split())  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        raise CdlError(f"{name} is missing or is not three numbers") from None
    if len(values) != 3 or not all(math.isfinite(v) for v in values):
        raise CdlError(f"{name} is missing or is not three numbers")
    if any(v < minimum or (exclusive and v == minimum) for v in values):
        raise CdlError(f"{name} {values} is out of range")
    return values  # type: ignore[return-value]


def _correction(node: ET.Element) -> Cdl:
    sop = _find(node, "SOPNode")
    slope = _triple(sop, "Slope", minimum=0.0, exclusive=False)
    offset = _triple(sop, "Offset", minimum=-math.inf, exclusive=False)
    power = _triple(sop, "Power", minimum=0.0, exclusive=True)
    sat = 1.0
    sat_node = _find(node, "SatNode")  # matches SATNode as well, the comparison ignores case
    if sat_node is not None:
        try:
            sat = float((_find(sat_node, "Saturation").text or "").strip())  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            raise CdlError("Saturation is not a number") from None
        if not math.isfinite(sat) or sat < 0:
            raise CdlError(f"Saturation {sat} is out of range")
    return Cdl(slope, offset, power, sat, node.get("id") or "")


def read_cdl(path: Path, *, wanted: tuple[str, ...] = ()) -> Cdl:
    """Read one correction. In a file that holds several, take the first of the wanted ids that exists."""
    path = Path(path)
    try:
        root = ET.fromstring(path.read_bytes())
    except OSError as error:
        raise CdlError(f"cannot read {path}: {error}") from None
    except ET.ParseError as error:
        raise CdlError(f"{path.name} is not valid XML: {error}") from None
    nodes = [node for node in root.iter() if _local(node.tag) == "ColorCorrection"]
    if not nodes:
        raise CdlError(f"{path.name} contains no ColorCorrection")
    chosen = nodes[0] if len(nodes) == 1 else None
    if chosen is None:
        by_id = {node.get("id"): node for node in nodes}
        chosen = next((by_id[name] for name in wanted if name in by_id), None)
    if chosen is None:
        raise CdlError(f"{path.name} holds {len(nodes)} corrections and none is named {' or '.join(wanted) or '?'}")
    try:
        return _correction(chosen)
    except CdlError as error:
        raise CdlError(f"{path.name}: {error}") from None
