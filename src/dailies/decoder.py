# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Decoder interface.

A decoder turns one camera clip into "something FFmpeg can read as picture".
That is either a producer process piped into FFmpeg, or a producer that writes
an intermediate to scratch which FFmpeg then reads. The render stage only sees
a DecodePlan, so REDline can be replaced by an R3D SDK decoder without any
other stage changing.

REDline flags differ between versions, so they come from the config as
templates. Nothing about REDline's command line is hardcoded here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dailies.commands.base import Command
from dailies.config import ShowConfig
from dailies.framing import decode_size

Size = tuple[int, int]
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class DecoderError(Exception):
    """The decoder cannot be built, or cannot plan this clip."""


@dataclass(frozen=True)
class DecodePlan:
    producer: Command | None  # None when FFmpeg reads the camera file itself, as it does for ProRes
    piped: bool  # True: producer stdout feeds FFmpeg stdin. False: producer runs first.
    input_args: tuple[str, ...]  # FFmpeg arguments for the picture input or inputs, each ending in -i <source>
    intermediates: tuple[Path, ...]  # scratch folders to remove once the render has succeeded
    input_count: int = 1  # picture inputs. More than one means a spanned movie, joined in the filter graph
    input_filters: tuple[str, ...] = ()  # applied before anything else, to reach 16-bit RGB correctly


@dataclass(frozen=True)
class ClipFacts:
    """What scan needs to know about one camera clip, however it was read."""

    clip_name: str  # full source clip name without span suffix or extension, the tape name
    camera_id: str
    roll: str
    clip: str
    width: int
    height: int
    fps: float
    frames: int
    start_tc: str  # the camera's default timecode track, which is what conform tools read
    tc_absolute: str | None
    tc_edge: str | None
    tc_preference: str  # "absolute" (time of day or external) or "edge" (record run)
    anamorphic: float
    iso: int | None  # a ProRes movie does not carry these three
    kelvin: int | None
    tint: float | None
    scene: str | None
    take: str | None
    dropped_frames: int
    camera_model: str
    codec: str = "R3D"


class Decoder(Protocol):
    name: str

    def version_command(self) -> Command: ...

    def parse_version(self, stdout: str) -> str: ...

    def metadata_command(self, clip: Path) -> Command: ...

    def parse_metadata(self, stdout: str) -> ClipFacts: ...

    def decoded_size(self, width: int, height: int) -> Size: ...

    def plan(self, clip: Path, *, clip_id: str, size: Size, fps: int, parts: tuple[Path, ...] = ()) -> DecodePlan: ...


class RedlineDecoder:
    name = "redline"

    def __init__(self, config: ShowConfig):
        self._exe = str(config.tools.redline.path)
        self._decode = config.decode
        self._scratch = config.paths.scratch

    def version_command(self) -> Command:
        return Command((self._exe, "--version"))

    def parse_version(self, stdout: str) -> str:
        raise DecoderError("REDline output parsing is not built: REDline is not installed to verify it against")

    def metadata_command(self, clip: Path) -> Command:
        args = self._decode.redline.metadata_args
        return Command((self._exe, *(a.format(clip=str(clip)) for a in args)))

    def parse_metadata(self, stdout: str) -> ClipFacts:
        raise DecoderError("REDline output parsing is not built: REDline is not installed to verify it against")

    def decoded_size(self, width: int, height: int) -> Size:
        return decode_size(width, height, self._decode.resolution)

    def plan(self, clip: Path, *, clip_id: str, size: Size, fps: int, parts: tuple[Path, ...] = ()) -> DecodePlan:
        if not _SAFE_ID.match(clip_id) or ".." in clip_id:
            raise DecoderError(f"clip id {clip_id!r} is not safe to use as a scratch folder name")
        red = self._decode.redline
        out_dir = self._scratch / clip_id
        values = {"clip": str(clip), "out_dir": str(out_dir), "out_name": clip_id,
                  "width": str(size[0]), "height": str(size[1])}
        producer = Command((self._exe, *(a.format(**values) for a in red.decode_args)))
        if self._decode.transport == "pipe":
            return DecodePlan(
                producer=producer, piped=True, intermediates=(),
                input_args=("-f", "rawvideo", "-pixel_format", "rgb48le", "-video_size", f"{size[0]}x{size[1]}",
                            "-framerate", str(fps), "-i", "pipe:0"),
            )
        sequence = out_dir / red.intermediate_pattern.format(out_name=clip_id)
        return DecodePlan(
            producer=producer, piped=False, intermediates=(out_dir,),
            input_args=("-framerate", str(fps), "-start_number", str(red.intermediate_start_number),
                        "-i", str(sequence)),
        )


def _blank_to_none(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


class SdkDecoder:
    """R3D SDK behind r3dtool, the small bridge in native/r3dtool. Output is JSON."""

    name = "sdk"

    def __init__(self, config: ShowConfig):
        self._exe = str(config.tools.r3dtool.path)
        self._resolution = getattr(getattr(config, "decode", None), "resolution", "quarter")

    def decoded_size(self, width: int, height: int) -> Size:
        return decode_size(width, height, self._resolution)

    def version_command(self) -> Command:
        return Command((self._exe, "version"))

    def parse_version(self, stdout: str) -> str:
        try:
            data = json.loads(stdout)
            sdk = data["sdk"].split(" [")[0]  # drop the library folder, it is a path and not a version
            return f"{data['tool_version']} / {sdk}"
        except (ValueError, KeyError, AttributeError, TypeError) as error:
            raise DecoderError(f"unreadable r3dtool version output: {error}") from None

    def metadata_command(self, clip: Path) -> Command:
        return Command((self._exe, "meta", str(clip)))

    def parse_metadata(self, stdout: str) -> ClipFacts:
        try:
            data = json.loads(stdout)
            md = data["metadata"]
            frames = int(data["frames"])
            if frames < 1:
                raise DecoderError(f"clip has no frames: {data.get('path')}")
            default_tc, absolute_tc, edge_tc = (data.get(key) or [None] for key in ("tc_default", "tc_absolute", "tc_edge"))
            if not default_tc[0]:
                raise KeyError("tc_default")
            return ClipFacts(
                clip_name=str(md["reel_id_full"]),
                camera_id=str(md["camera_id"]),
                roll=str(md["reel_id"]),
                clip=f"C{md['clip_id']}",
                width=int(data["width"]),
                height=int(data["height"]),
                fps=int(md["framerate_numerator"]) / int(md["framerate_denominator"]),
                frames=frames,
                start_tc=default_tc[0],
                tc_absolute=absolute_tc[0],
                tc_edge=edge_tc[0],
                tc_preference="absolute" if int(md["user_timecode_preference"]) == 1 else "edge",
                anamorphic=float(md.get("pixel_aspect_ratio") or 1.0),
                iso=int(md["iso"]),
                kelvin=int(md["white_balance_kelvin"]),
                tint=float(md["white_balance_tint"]),
                scene=_blank_to_none(md.get("user_scene")),
                take=_blank_to_none(md.get("user_take")),
                dropped_frames=int(md.get("dropped_frame_count") or 0),
                camera_model=str(md.get("camera_model") or ""),
            )
        except DecoderError:
            raise
        except (ValueError, KeyError, TypeError, ZeroDivisionError, AttributeError) as error:
            raise DecoderError(f"unreadable r3dtool metadata output: {type(error).__name__} {error}") from None

    def plan(self, clip: Path, *, clip_id: str, size: Size, fps: int, parts: tuple[Path, ...] = ()) -> DecodePlan:
        raise DecoderError("r3dtool decode is built in the render step, use decode.engine redline for a render dry run")


_RED_NAME = re.compile(r"^([A-Z])(\d{3})_C(\d{3})")
_SPAN_SUFFIX = re.compile(r"_\d{3}$")


class FfmpegDecoder:
    """ProRes movies. ffprobe reads the metadata and FFmpeg decodes the file itself, at the size it was recorded.

    ProRes 422 HQ is 10 bit 4:2:2 in legal range. It is converted to 16-bit RGB with the BT.709 matrix stated
    explicitly, so the colour chain sees the same kind of picture it gets from an R3D decode.
    """

    name = "ffmpeg"

    def __init__(self, config: ShowConfig):
        self._ffprobe = str(config.tools.ffprobe.path)

    def version_command(self) -> Command:
        return Command((self._ffprobe, "-version"))

    def parse_version(self, stdout: str) -> str:
        match = re.match(r"^ffprobe version (\S+)", stdout)
        if not match:
            raise DecoderError("unreadable ffprobe version output")
        return match.group(1)

    def metadata_command(self, clip: Path) -> Command:
        return Command((self._ffprobe, "-v", "error", "-show_format", "-show_streams", "-print_format", "json",
                        str(clip)))

    def parse_metadata(self, stdout: str) -> ClipFacts:
        try:
            data = json.loads(stdout)
            video = next(s for s in data["streams"] if s.get("codec_type") == "video")
            if video.get("codec_name") != "prores":
                raise DecoderError(f"movie metadata: expected ProRes, the video codec is {video.get('codec_name')}")
            timecode = next((s["tags"]["timecode"] for s in [video, *data["streams"]]
                             if s.get("tags", {}).get("timecode")), None) or data["format"].get("tags", {}).get("timecode")
            if not timecode:
                raise DecoderError("movie metadata: the file has no timecode track")
            numerator, denominator = (int(v) for v in video["r_frame_rate"].split("/"))
            sar_w, sar_h = (int(v) for v in (video.get("sample_aspect_ratio") or "1:1").split(":"))
            name = _SPAN_SUFFIX.sub("", Path(data["format"]["filename"]).stem)
            red = _RED_NAME.match(name)
            tags = data["format"].get("tags", {})
            return ClipFacts(
                clip_name=name,
                camera_id=red.group(1) if red else name[:1],
                roll=red.group(2) if red else "",
                clip=f"C{red.group(3)}" if red else "",
                width=int(video["width"]), height=int(video["height"]),
                fps=numerator / denominator, frames=int(video["nb_frames"]),
                start_tc=timecode, tc_absolute=None, tc_edge=None, tc_preference="container",
                anamorphic=sar_w / sar_h if sar_h else 1.0,
                iso=None, kelvin=None, tint=None, scene=None, take=None, dropped_frames=0,
                camera_model=str(tags.get("com.apple.quicktime.model") or ""),
                codec=f"ProRes {video.get('profile') or ''}".strip(),
            )
        except DecoderError:
            raise
        except (ValueError, KeyError, TypeError, StopIteration, ZeroDivisionError, AttributeError) as error:
            raise DecoderError(f"unreadable movie metadata: {type(error).__name__} {error}") from None

    def decoded_size(self, width: int, height: int) -> Size:
        return decode_size(width, height, "full")

    def plan(self, clip: Path, *, clip_id: str, size: Size, fps: int, parts: tuple[Path, ...] = ()) -> DecodePlan:
        files = parts or (clip,)
        args: list[str] = []
        for part in files:
            args += ["-i", str(part)]
        return DecodePlan(producer=None, piped=False, input_args=tuple(args), intermediates=(), input_count=len(files),
                          input_filters=("scale=in_color_matrix=bt709:in_range=tv",))


def make_decoder(config: ShowConfig) -> Decoder:
    """The show's R3D decoder."""
    return SdkDecoder(config) if config.decode.engine == "sdk" else RedlineDecoder(config)


def decoder_for(config: ShowConfig, source: Path) -> Decoder:
    """The decoder follows the file: R3D goes to the show's R3D engine, a movie goes to FFmpeg."""
    extension = Path(source).suffix.lower()
    if extension == ".r3d":
        return make_decoder(config)
    if extension == ".mov":
        return FfmpegDecoder(config)
    raise DecoderError(f"no decoder for {Path(source).name}")
