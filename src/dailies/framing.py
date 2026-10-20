# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Framing maths. Pure functions, integer arithmetic, no media needed.

The crop is a show constant applied as a centred percentage on both axes. It
is computed in decode space, which is equivalent to applying the frameline to
the full sensor image and then scaling down by the decode divisor.

Rounding rule: every dimension is rounded down to an even number and can never
exceed its source. A crop or a scale that would distort the picture raises
FramingError so the clip fails loudly instead of being squeezed.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

DECODE_DIVISORS = {"full": 1, "half": 2, "quarter": 4, "eighth": 8}
FIT_MODES = ("width", "height", "exact")
DEFAULT_ASPECT_TOLERANCE = 0.005

Size = tuple[int, int]


class FramingError(ValueError):
    """The requested framing cannot be produced without distorting the picture."""


def _floor_even(value: Fraction | int) -> int:
    whole = int(value)  # int() truncates, values here are never negative
    return whole - (whole % 2)


def decode_size(source_width: int, source_height: int, resolution: str) -> Size:
    """Size of the decoded frame for a named decode resolution."""
    try:
        divisor = DECODE_DIVISORS[resolution]
    except KeyError:
        raise FramingError(
            f"unknown decode resolution {resolution!r}, expected one of {sorted(DECODE_DIVISORS)}"
        ) from None
    return (
        _floor_even(Fraction(source_width, divisor)),
        _floor_even(Fraction(source_height, divisor)),
    )


@dataclass(frozen=True)
class Framing:
    decode: Size
    crop: Size
    crop_offset: Size
    scale: Size
    pad_offset: Size
    output: Size
    fit: str
    desqueezed: Size | None = None

    def filters(self, final_scale_options: tuple[str, ...] = ()) -> tuple[str, ...]:
        """FFmpeg filters in application order: desqueeze, crop, scale, pad.

        Options for the final scale let the caller fold the RGB to YUV conversion
        into the same scaler pass as the resize."""
        chain: list[str] = []
        if self.desqueezed is not None:
            chain.append(f"scale={self.desqueezed[0]}:{self.desqueezed[1]}:flags=lanczos")
        chain.append(f"crop={self.crop[0]}:{self.crop[1]}:{self.crop_offset[0]}:{self.crop_offset[1]}")
        chain.append(":".join((f"scale={self.scale[0]}:{self.scale[1]}:flags=lanczos", *final_scale_options)))
        if self.scale != self.output:
            chain.append(
                f"pad={self.output[0]}:{self.output[1]}:{self.pad_offset[0]}:{self.pad_offset[1]}:color=black"
            )
        return tuple(chain)

    def to_dict(self) -> dict:
        block = {
            "decode": list(self.decode),
            "crop": list(self.crop),
            "crop_offset": list(self.crop_offset),
            "scale": list(self.scale),
            "pad_offset": list(self.pad_offset),
            "output": list(self.output),
            "fit": self.fit,
        }
        if self.desqueezed is not None:
            block["desqueezed"] = list(self.desqueezed)
        return block

    @classmethod
    def from_dict(cls, block: dict) -> "Framing":
        desqueezed = block.get("desqueezed")
        return cls(
            decode=tuple(block["decode"]),
            crop=tuple(block["crop"]),
            crop_offset=tuple(block["crop_offset"]),
            scale=tuple(block["scale"]),
            pad_offset=tuple(block["pad_offset"]),
            output=tuple(block["output"]),
            fit=block["fit"],
            desqueezed=tuple(desqueezed) if desqueezed is not None else None,
        )


def compute_framing(
    decode: Size,
    *,
    crop_percent: float,
    output: Size,
    fit: str,
    anamorphic: float = 1.0,
    aspect_tolerance: float = DEFAULT_ASPECT_TOLERANCE,
) -> Framing:
    """Resolve crop, scale and pad for one decode size and one deliverable raster."""
    if fit not in FIT_MODES:
        raise FramingError(f"unknown fit mode {fit!r}, expected one of {list(FIT_MODES)}")
    if not 0.0 <= crop_percent < 100.0:
        raise FramingError(f"crop_percent must be at least 0 and below 100, got {crop_percent}")
    out_w, out_h = output
    if out_w % 2 or out_h % 2 or out_w <= 0 or out_h <= 0:
        raise FramingError(f"output raster must have positive even dimensions, got {out_w}x{out_h}")

    desqueezed: Size | None = None
    work_w, work_h = decode
    if anamorphic != 1.0:
        desqueezed = (_floor_even(Fraction(str(anamorphic)) * work_w), work_h)
        work_w = desqueezed[0]

    keep = Fraction(100) - Fraction(str(crop_percent))
    crop_w = _floor_even(work_w * keep / 100)
    crop_h = _floor_even(work_h * keep / 100)

    if fit == "height":
        # trim the sides further so the crop has exactly the raster's shape
        crop_w = min(crop_w, _floor_even(Fraction(crop_h * out_w, out_h)))

    scale_w, scale_h = out_w, _floor_even(Fraction(out_w * crop_h, crop_w))
    if scale_h > out_h:
        if fit == "width":
            raise FramingError(
                f"cropped picture {crop_w}x{crop_h} is taller than the {out_w}x{out_h} raster, "
                "fit width would overflow the height"
            )
        scale_h = out_h

    distortion = abs(Fraction(scale_w * crop_h, scale_h * crop_w) - 1)
    if distortion > Fraction(str(aspect_tolerance)):
        raise FramingError(
            f"aspect mismatch: crop {crop_w}x{crop_h} into {scale_w}x{scale_h} distorts the picture "
            f"by {float(distortion):.2%}, tolerance is {aspect_tolerance:.2%}"
        )
    if fit == "exact" and (scale_w, scale_h) != (out_w, out_h):
        raise FramingError(
            f"aspect mismatch: crop {crop_w}x{crop_h} does not fill the {out_w}x{out_h} raster, "
            "choose fit width or fit height"
        )

    return Framing(
        decode=decode,
        crop=(crop_w, crop_h),
        crop_offset=((work_w - crop_w) // 2, (work_h - crop_h) // 2),
        scale=(scale_w, scale_h),
        pad_offset=((out_w - scale_w) // 2, (out_h - scale_h) // 2),
        output=output,
        fit=fit,
        desqueezed=desqueezed,
    )
