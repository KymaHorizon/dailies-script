# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""FFmpeg filter string builders. Pure functions over strings.

Escaping has two levels, because the filtergraph is parsed twice. The option
parser of each filter splits on colons and understands backslash escapes and
single quotes. Before that, the graph parser splits on commas, semicolons and
brackets and understands the same quoting. Values are escaped for the option
parser first, then wrapped in single quotes for the graph parser. The whole
graph is passed to FFmpeg as one argv element, so there is no shell level.
"""

from __future__ import annotations

from pathlib import Path

from dailies.timecode import TimecodeError, tc_to_frames

_X = {"left": "{m}", "center": "(w-tw)/2", "right": "w-tw-{m}"}
_Y = {"top": "{m}", "bottom": "h-lh-{m}"}


class FilterError(ValueError):
    """A filter that cannot be expressed safely or exactly."""


def filter_value(text: str) -> str:
    """Escape and quote one filter option value."""
    if "\n" in text or "\r" in text:
        raise FilterError(f"line break in filter value {text!r}")
    option_level = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return "'" + option_level.replace("'", "'\\''") + "'"


def drawtext(*, font: Path, text: str, position: str, fontsize: int, margin: int,
             timecode: str | None = None, fps: int | None = None) -> str:
    """One burn-in field. A running timecode, if given, is appended after the text by FFmpeg."""
    try:
        vertical, horizontal = position.split("_")
        x = _X[horizontal].format(m=margin)
        y = _Y[vertical].format(m=margin)
    except (ValueError, KeyError):
        raise FilterError(f"unknown burn-in position {position!r}") from None
    parts = [
        f"drawtext=fontfile={filter_value(str(font))}",
        f"text={filter_value(text)}",
        "expansion=none",
        f"fontsize={fontsize}",
        "fontcolor=white",
        "box=1",
        "boxcolor=black@0.5",
        "boxborderw=4",
        f"x={x}",
        f"y={y}",
    ]
    if timecode is not None:
        if fps is None:
            raise FilterError("a running timecode needs the show fps")
        try:
            tc_to_frames(timecode, fps=fps)
        except TimecodeError as error:
            raise FilterError(str(error)) from None
        parts += [f"timecode={filter_value(timecode)}", f"timecode_rate={fps}"]
    return ":".join(parts)


def sync_audio_filters(*, offset_samples: int, picture_samples: int, sample_rate: int) -> tuple[str, ...]:
    """Sample-accurate sync, then pad and trim to exactly the picture length.

    Positive offset: sound started before picture, trim the head.
    Negative offset: sound started late, delay the head by that many samples.
    """
    if isinstance(offset_samples, bool) or not isinstance(offset_samples, int):
        raise FilterError(f"offset_samples must be a whole number of samples, got {offset_samples!r}")
    if isinstance(picture_samples, bool) or not isinstance(picture_samples, int) or picture_samples <= 0:
        raise FilterError(f"picture_samples must be a positive whole number, got {picture_samples!r}")
    chain: list[str] = []
    if offset_samples > 0:
        chain += [f"atrim=start_sample={offset_samples}", "asetpts=N/SR/TB"]
    elif offset_samples < 0:
        chain.append(f"adelay={-offset_samples}S:all=1")
    chain += [f"apad=whole_len={picture_samples}", f"atrim=end_sample={picture_samples}"]
    return tuple(chain)


def mix_pan(tracks: tuple[int, ...], *, layout: str, channels: int) -> str:
    """Pick mix tracks (counted from 1) out of a poly WAV."""
    for track in tracks:
        if track > channels:
            raise FilterError(f"mix track {track} does not exist, the sound file has {channels} channel(s)")
    if layout == "mono":
        if len(tracks) != 1:
            raise FilterError(f"a mono pan takes exactly one track, got {tracks!r}")
        return f"pan=mono|c0=c{tracks[0] - 1}"
    if layout == "stereo":
        left = tracks[0] - 1
        right = tracks[1] - 1 if len(tracks) > 1 else left
        return f"pan=stereo|c0=c{left}|c1=c{right}"
    raise FilterError(f"unknown layout {layout!r}")


# Rec.709 luma weights, as the ASC CDL specification requires for the saturation step
_LUMA = (0.2126, 0.7152, 0.0722)


def _number(value: float) -> str:
    """Plain decimal. FFmpeg's expression parser is not Python and must never see 1e-05."""
    text = f"{float(value):.10f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def cdl_filters(cdl) -> tuple[str, ...]:
    """An ASC CDL as FFmpeg filters on 16-bit RGB: one lookup per channel for slope, offset and power,
    then a 3x3 matrix for saturation.

    Measured against OpenColorIO through FFmpeg at 16 bit: within 0.3 ten bit codes, where a CDL baked
    into a 65 point cube is off by up to 20. The lookup clamps to 0 to 1 after the power function, which
    is the strict ASC behaviour. A neutral CDL produces no filters at all.
    """
    chain: list[str] = []
    if (cdl.slope, cdl.offset, cdl.power) != ((1, 1, 1), (0, 0, 0), (1, 1, 1)):
        channels = [
            f"{name}='clip(pow(max(val/65535*{_number(slope)}+({_number(offset)}),0),{_number(power)}),0,1)*65535'"
            for name, slope, offset, power in zip("rgb", cdl.slope, cdl.offset, cdl.power)
        ]
        chain.append("lutrgb=" + ":".join(channels))
    if cdl.sat != 1:
        keep = 1.0 - cdl.sat
        cells = []
        for row_index, row in enumerate("rgb"):
            for col_index, col in enumerate("rgb"):
                value = _LUMA[col_index] * keep + (cdl.sat if row_index == col_index else 0.0)
                cells.append(f"{row}{col}={_number(round(value, 10))}")
        chain.append("colorchannelmixer=" + ":".join(cells))
    return tuple(chain)
