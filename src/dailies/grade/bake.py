# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Bake one clip's colour decisions into a single 3D cube.

Order, fixed: CDL, then the camera look, then the show LUT, then the output
transform if the show needs one. The chain is built as one OpenColorIO
transform group and evaluated on an identity lattice, so the cube is exactly
what OCIO computes at every lattice point, with no shaper and no second
resampling step in between.

The cube is cached by the hash of what went into it: the CDL values, the
content of every LUT file, the output transform, the clamp setting and the
cube size. File names, paths and dates are not part of the hash, so moving the
LUT folder changes nothing and editing one LUT byte changes everything it
should. Most clips in a day share a look, so a day usually needs a handful of
cubes, and a regrade of one scene bakes one new cube.
"""

from __future__ import annotations

import array
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from dailies.config import hash_of
from dailies.grade.cdl import Cdl

BAKER_VERSION = 1  # bump when the baking method itself changes, so old cubes are not reused


class GradeError(Exception):
    """The colour chain for this clip cannot be built."""


@dataclass(frozen=True)
class Recipe:
    cdl: Cdl | None
    camera_look: Path | None
    show_lut: Path
    output_transform: str | None
    decode_space: str
    ocio_config: Path
    bake_size: int
    cdl_clamp: bool


@dataclass(frozen=True)
class BakeResult:
    path: Path
    lut_hash: str
    baked: bool  # False when an existing cube was reused


def file_hash(path: Path) -> str:
    try:
        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as error:
        raise GradeError(f"cannot read {path}: {error}") from None


def recipe_hash(recipe: Recipe, apply: str = "baked") -> str:
    """Identity of the picture this recipe produces. In exact mode the cube size, the clamp setting and
    the baker version do not exist, so they are left out and changing them re-renders nothing."""
    content = {
        "cdl": recipe.cdl.to_dict() if recipe.cdl is not None else None,
        "camera_look": file_hash(recipe.camera_look) if recipe.camera_look else None,
        "show_lut": file_hash(recipe.show_lut),
        "output_transform": recipe.output_transform,
        "decode_space": recipe.decode_space if recipe.output_transform else None,
        "ocio_config": file_hash(recipe.ocio_config) if recipe.output_transform else None,
    }
    if apply == "baked":
        content.update(baker=BAKER_VERSION, cdl_clamp=recipe.cdl_clamp, size=recipe.bake_size)
    return hash_of({"apply": apply, **content})


def _processor(recipe: Recipe):
    import PyOpenColorIO as ocio

    group = ocio.GroupTransform()
    if recipe.cdl is not None:
        cdl = ocio.CDLTransform()
        cdl.setSlope(list(recipe.cdl.slope))
        cdl.setOffset(list(recipe.cdl.offset))
        cdl.setPower(list(recipe.cdl.power))
        cdl.setSat(recipe.cdl.sat)
        cdl.setStyle(ocio.CDL_ASC if recipe.cdl_clamp else ocio.CDL_NO_CLAMP)
        group.appendTransform(cdl)
    for lut in (recipe.camera_look, recipe.show_lut):
        if lut is not None:
            group.appendTransform(ocio.FileTransform(str(lut), interpolation=ocio.INTERP_TETRAHEDRAL))
    try:
        if recipe.output_transform is None:
            config = ocio.Config.CreateRaw()
        else:
            config = ocio.Config.CreateFromFile(str(recipe.ocio_config))
            group.appendTransform(ocio.ColorSpaceTransform(src=recipe.decode_space, dst=recipe.output_transform))
        return config.getProcessor(group).getDefaultCPUProcessor()
    except ocio.Exception as error:
        raise GradeError(f"OpenColorIO could not build the colour chain: {error}") from None


def _describe(recipe: Recipe, lut_hash: str) -> list[str]:
    lines = [f"# baked by dailies, baker {BAKER_VERSION}", f"# recipe {lut_hash}"]
    if recipe.cdl is not None:
        c = recipe.cdl
        lines.append(f"# cdl {c.id}: slope {' '.join(map(str, c.slope))} offset {' '.join(map(str, c.offset))} "
                     f"power {' '.join(map(str, c.power))} sat {c.sat}, clamp {recipe.cdl_clamp}")
    else:
        lines.append("# cdl none")
    lines.append(f"# camera look {recipe.camera_look.name if recipe.camera_look else 'none'}")
    lines.append(f"# show lut {recipe.show_lut.name}")
    lines.append(f"# output transform {recipe.output_transform or 'none'}")
    return lines


def bake(recipe: Recipe, cache_dir: Path) -> BakeResult:
    lut_hash = recipe_hash(recipe)
    path = Path(cache_dir) / f"{lut_hash[7:23]}.cube"
    if path.is_file():
        return BakeResult(path, lut_hash, baked=False)

    size = recipe.bake_size
    step = 1.0 / (size - 1)
    lattice = array.array("f")
    for b in range(size):
        for g in range(size):
            for r in range(size):  # red changes fastest, as the .cube format requires
                lattice.extend((r * step, g * step, b * step))
    _processor(recipe).applyRGB(lattice)

    lines = _describe(recipe, lut_hash) + [f"LUT_3D_SIZE {size}", ""]
    lines += [f"{lattice[i]:.8f} {lattice[i + 1]:.8f} {lattice[i + 2]:.8f}" for i in range(0, len(lattice), 3)]
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temp, "w") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return BakeResult(path, lut_hash, baked=True)
