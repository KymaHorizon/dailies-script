# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Names built from config templates and manifest facts. Nothing reads the filesystem."""

from __future__ import annotations

from dataclasses import dataclass

from dailies.config import RUNNING_TC, BurnIn, Naming

_UNSYNCED = {"mos": "MOS"}
_AUDIBLE = ("matched", "partial_overlap", "multiple_candidates")


class NamingError(ValueError):
    """A resolved name that cannot be used as a file or clip name."""


@dataclass(frozen=True)
class DayContext:
    show: str
    day: str  # ISO date, as in the manifest header
    day_code: str
    unit: str

    @property
    def date(self) -> str:
        return self.day.replace("-", "")


@dataclass(frozen=True)
class ClipNames:
    tape: str
    avid_clip: str
    used_fallback: bool


@dataclass(frozen=True)
class BurnInText:
    text: str
    running_timecode: str | None = None


def _values(day: DayContext, clip_id: str, picture: dict) -> dict[str, str]:
    camera = picture.get("camera") or ""
    return {
        "show": day.show, "date": day.date, "day_code": day.day_code, "unit": day.unit,
        "clip_name": clip_id, "camera": camera, "camera_lower": camera.lower(),
        "roll": picture.get("roll") or "", "clip": picture.get("clip") or "",
        "scene": picture.get("scene") or "", "take": picture.get("take") or "",
    }


def _safe(name: str, what: str) -> str:
    if "/" in name or "\\" in name or "\x00" in name:
        raise NamingError(f"{what} {name!r} contains a path separator")
    return name


def day_folder(naming: Naming, day: DayContext) -> str:
    return _safe(naming.day_folder.format(**_values(day, "", {})), "day folder")


def clip_names(naming: Naming, day: DayContext, clip_id: str, picture: dict) -> ClipNames:
    values = _values(day, clip_id, picture)
    has_slate = bool(values["scene"] and values["take"])
    template = naming.avid_clip if has_slate else naming.avid_clip_fallback
    return ClipNames(
        tape=_safe(naming.tape.format(**values), "tape name"),
        avid_clip=_safe(template.format(**values), "Avid clip name"),
        used_fallback=not has_slate,
    )


def burn_in_texts(burn_in: BurnIn, day: DayContext, clip_id: str, picture: dict,
                  sound: dict) -> dict[str, BurnInText]:
    values = _values(day, clip_id, picture)
    values["tape"] = clip_id
    values["soundroll"] = sound.get("soundroll") or ""
    if sound["status"] in _AUDIBLE and sound.get("start_tc"):
        values["sound_tc"] = sound["start_tc"]
    else:
        values["sound_tc"] = _UNSYNCED.get(sound["status"], "NO SYNC")
    marker = "{" + RUNNING_TC + "}"
    resolved: dict[str, BurnInText] = {}
    for position, template in burn_in.fields.items():
        if template.endswith(marker):
            label = template[: -len(marker)].format(**values)
            resolved[position] = BurnInText(label, running_timecode=picture["start_tc"])
        else:
            resolved[position] = BurnInText(template.format(**values))
    return resolved
