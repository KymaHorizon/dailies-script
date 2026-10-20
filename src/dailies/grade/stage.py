# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Grade stage: resolve the colour decisions for each clip and record them in the manifest.

Order of the chain, always: CDL, then the camera look for that camera letter, then the show LUT,
then the output transform if the show has one. How the chain reaches the picture is the show's
decision (colour.apply): applied step by step at render time, or baked into one cube here.

A missing CDL is a flag, not a failure. The neutral default is used if the show has one, and the
clip is flagged either way so that a CDL which should have arrived is never silently neutral. An
unreadable CDL is flagged too, and is never replaced by a less specific one: a wrong grade that
looks plausible is worse than a neutral one that is reported.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from dailies.config import ShowConfig
from dailies.grade.bake import GradeError, Recipe, bake, recipe_hash
from dailies.grade.cdl import Cdl, CdlError, read_cdl
from dailies.grade.cube import CubeError, read_cube
from dailies.manifest import Manifest, derive_status
from dailies.naming import DayContext, day_folder

GRADE_FLAGS = {"missing_cdl", "cdl_unreadable"}
CDL_EXTENSIONS = (".cdl", ".ccc", ".cc")


class GradeAbort(Exception):
    """Something every clip depends on is unusable. Exit code 2."""


@dataclass
class GradeSummary:
    by_cdl: dict[str, int] = field(default_factory=dict)
    cubes_baked: int = 0
    warnings: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResolvedCdl:
    status: str  # found | default | missing | unreadable
    cdl: Cdl | None
    source: Path | None
    note: str


def _inside(folder: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def resolve_cdl(config: ShowConfig, day: DayContext, clip_id: str, picture: dict) -> ResolvedCdl:
    colour = config.colour
    values = {"day_folder": day_folder(config.naming, day), "date": day.date, "day_code": day.day_code,
              "unit": day.unit, "clip_name": clip_id, "scene": picture.get("scene") or "",
              "camera": picture.get("camera") or "", "reel": picture.get("reel") or ""}
    wanted = tuple(name for name in (clip_id, picture.get("scene")) if name)
    for template in colour.cdl_search:
        if any("{" + key + "}" in template and not value for key, value in values.items()):
            continue  # a template that needs a scene is skipped for a clip that has none
        stem = colour.cdl_dir / template.format(**values)
        for extension in CDL_EXTENSIONS:
            path = stem.with_name(stem.name + extension)
            if not path.is_file() or not _inside(colour.cdl_dir, path):
                continue
            try:
                return ResolvedCdl("found", read_cdl(path, wanted=wanted), path, f"CDL {path.name}")
            except CdlError as error:
                return ResolvedCdl("unreadable", None, path, f"unreadable CDL, show LUT only: {error}")
    if colour.cdl_default is not None:
        for extension in CDL_EXTENSIONS:
            path = colour.cdl_dir / (colour.cdl_default + extension)
            if path.is_file():
                try:
                    cdl = read_cdl(path, wanted=(colour.cdl_default,))
                    return ResolvedCdl("default", cdl, path, f"no CDL for this clip, neutral default {path.name} used")
                except CdlError as error:
                    return ResolvedCdl("unreadable", None, path, f"unreadable default CDL, show LUT only: {error}")
        return ResolvedCdl("missing", None, None,
                           f"no CDL for this clip and the default {colour.cdl_default} was not found in {colour.cdl_dir}")
    return ResolvedCdl("missing", None, None, "no CDL for this clip, show LUT only")


def _bake_warnings(config: ShowConfig) -> list[str]:
    """A baked cube coarser than, or not aligned with, a source LUT throws picture information away."""
    warnings = []
    colour = config.colour
    luts = [colour.show_lut, *(look for look in colour.camera_looks.values() if look is not None)]
    for lut in dict.fromkeys(luts):
        try:
            size = read_cube(lut).size
        except CubeError as error:
            raise GradeAbort(str(error)) from None
        if colour.bake_size < size:
            warnings.append(f"colour.bake_size {colour.bake_size} is smaller than {lut.name} ({size} points), "
                            "the bake will lose accuracy")
        elif (colour.bake_size - 1) % (size - 1):
            warnings.append(f"colour.bake_size {colour.bake_size} does not line up with {lut.name} ({size} points), "
                            f"use {2 * (size - 1) + 1} or {size}")
    return warnings


def run_grade(config: ShowConfig, manifest: Manifest, day: DayContext, *, clip_filter: str | None = None,
              dry_run: bool = False) -> GradeSummary:
    summary = GradeSummary()
    colour = config.colour
    targets = tuple(config.deliverables)
    if colour.apply == "baked":
        summary.warnings += _bake_warnings(config)

    for clip in manifest.clips:
        clip_id, picture = clip["id"], clip["picture"]
        if clip_filter is not None and not fnmatch.fnmatchcase(clip_id, clip_filter):
            continue
        resolved = resolve_cdl(config, day, clip_id, picture)
        look = colour.camera_looks.get(picture["camera"])
        recipe = Recipe(resolved.cdl, look, colour.show_lut, colour.output_transform, colour.decode_space,
                        colour.ocio_config, colour.bake_size, colour.cdl_clamp)
        try:
            block = {
                "apply": colour.apply, "lut_hash": recipe_hash(recipe, colour.apply), "show_lut": str(colour.show_lut),
                "camera_look": str(look) if look else None,
                "cdl": resolved.cdl.to_dict() if resolved.cdl else None,
                "cdl_source": str(resolved.source) if resolved.source and resolved.cdl else None,
                "note": resolved.note,
            }
            if colour.apply == "baked" and not dry_run:
                result = bake(recipe, colour.lut_cache)
                block["baked_lut"] = str(result.path)
                summary.cubes_baked += result.baked
        except GradeError as error:
            raise GradeAbort(str(error)) from None

        flags = {"missing": ["missing_cdl"], "default": ["missing_cdl"], "unreadable": ["cdl_unreadable"]}
        summary.by_cdl[resolved.status] = summary.by_cdl.get(resolved.status, 0) + 1
        summary.lines.append(f"{clip_id}  {resolved.status}  {resolved.note}")
        if resolved.status != "found":
            summary.warnings.append(f"{clip_id}: {resolved.note}")
        if dry_run:
            continue
        manifest.set_block(clip_id, stage="grade", name="colour", block=block)
        manifest.replace_flags(clip_id, owned=GRADE_FLAGS, current=flags.get(resolved.status, []))
        manifest.set_status(clip_id, derive_status(manifest.clip(clip_id), targets))
    return summary
