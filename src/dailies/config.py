# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Show config: one YAML per show, validated completely before any work starts.

Rules:
- Every problem is collected and reported together, each with its dotted key.
- Unknown keys are errors, so a typo can never silently fall back to a default.
- Decisions that change the picture have no default. They must be stated.
- The loaded config is immutable. Stages receive it, they never alter it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from dailies.framing import DECODE_DIVISORS, FIT_MODES, Framing, FramingError, compute_framing, decode_size

SHOW_FPS = 50

CODEC_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "dnxhr_lb": {"containers": ("mxf_opatom",), "pix_fmts": ("yuv422p",)},
    "dnxhr_sq": {"containers": ("mxf_opatom",), "pix_fmts": ("yuv422p",)},
    "dnxhr_hq": {"containers": ("mxf_opatom",), "pix_fmts": ("yuv422p",)},
    "dnxhr_hqx": {"containers": ("mxf_opatom",), "pix_fmts": ("yuv422p10le",)},
    "dnxhr_444": {"containers": ("mxf_opatom",), "pix_fmts": ("yuv444p10le",)},
    "h264": {"containers": ("mp4", "mov"), "pix_fmts": ("yuv420p",)},
}
PCM_CODECS = ("pcm_s16le", "pcm_s24le")
_AAC = re.compile(r"^aac_(\d+)k$")

BURN_IN_POSITIONS = ("top_left", "top_center", "top_right", "bottom_left", "bottom_center", "bottom_right")
RUNNING_TC = "source_tc"
BURN_IN_PLACEHOLDERS = frozenset(
    {"show", "date", "day_code", "unit", "clip_name", "tape", "camera", "roll", "clip", "scene", "take",
     "soundroll", "sound_tc", RUNNING_TC}
)
SLATE_PLACEHOLDERS = frozenset({"scene", "take"})
NAMING_PLACEHOLDERS = frozenset(
    {"show", "date", "day_code", "unit", "clip_name", "camera", "camera_lower", "roll", "clip"}
) | SLATE_PLACEHOLDERS
REDLINE_PLACEHOLDERS = frozenset({"clip", "out_dir", "out_name", "width", "height"})
CDL_SEARCH_PLACEHOLDERS = frozenset({"day_folder", "date", "day_code", "unit", "clip_name", "scene", "camera", "reel"})
SCAN_DIR_PLACEHOLDERS = frozenset({"show", "date", "day_code", "unit", "day_folder"})
ENGINE_TOOL = {"redline": "redline", "sdk": "r3dtool"}


class ConfigError(Exception):
    """The show config is unusable. Carries every problem found, not just the first."""

    def __init__(self, problems: list[str], source: Path | None = None):
        self.problems = problems
        self.source = source
        where = f" in {source}" if source else ""
        super().__init__(f"{len(problems)} config problem(s){where}:\n" + "\n".join(f"  - {p}" for p in problems))


# ------------------------------------------------------------------- model


@dataclass(frozen=True)
class Show:
    name: str
    code: str
    fps: int
    audio_rate: int
    timecode: str
    cameras: tuple[str, ...]
    units: tuple[str, ...]

    @property
    def samples_per_frame(self) -> int:
        return self.audio_rate // self.fps


@dataclass(frozen=True)
class Tool:
    path: Path
    version: str


@dataclass(frozen=True)
class Tools:
    ffmpeg: Tool
    ffprobe: Tool
    redline: Tool | None  # required when decode.engine is redline
    r3dtool: Tool | None  # required when decode.engine is sdk


@dataclass(frozen=True)
class FramingConfig:
    crop_percent: float
    crop_order: str
    fit: str
    aspect_tolerance: float


@dataclass(frozen=True)
class Colour:
    ocio_config: Path
    decode_space: str
    output_transform: str | None
    show_lut: Path
    camera_looks: dict[str, Path | None]
    cdl_dir: Path
    lut_cache: Path
    bake_size: int
    cdl_clamp: bool
    apply: str  # "exact": CDL as filters plus untouched LUTs. "baked": one OCIO cube, as the original spec describes
    cdl_search: tuple[str, ...]  # templates under cdl_dir, first match wins
    cdl_default: str | None  # name of the neutral CDL used when nothing matches


@dataclass(frozen=True)
class RedlineConfig:
    metadata_args: tuple[str, ...]
    decode_args: tuple[str, ...]
    intermediate_pattern: str
    intermediate_start_number: int


@dataclass(frozen=True)
class Decode:
    engine: str
    resolution: str
    bit_depth: int
    transport: str
    sources: dict[str, str]  # camera letter -> "r3d" or "prores". Scan flags a camera that delivers anything else
    expected_resolution: dict[str, tuple[int, int]]  # camera letter -> recorded width and height
    camera_letter_from: str
    redline: RedlineConfig | None  # flag templates, only needed by the redline engine


@dataclass(frozen=True)
class Sound:
    mix_tracks: tuple[int, ...]
    tc_window_hours: float
    filename_pattern: str | None  # regex with scene and take groups, used when iXML and bext have neither
    mos_cameras: tuple[str, ...]  # cameras that never have sync sound


@dataclass(frozen=True)
class Scan:
    camera_dir: str  # template for the day's camera folder, relative to paths.camera_root
    sound_dir: str  # same, relative to paths.sound_root


@dataclass(frozen=True)
class Deliverable:
    name: str
    codec: str
    container: str
    width: int
    height: int
    pix_fmt: str
    crf: int | None
    audio_codec: str
    audio_bitrate_k: int | None
    burn_in: bool


@dataclass(frozen=True)
class BurnIn:
    font: Path
    size_ratio: float
    margin_ratio: float
    fields: dict[str, str]


@dataclass(frozen=True)
class Naming:
    day_folder: str
    tape: str
    avid_clip: str
    avid_clip_fallback: str


@dataclass(frozen=True)
class Paths:
    camera_root: Path
    sound_root: Path
    output_root: Path
    scratch: Path
    manifest_dir: Path
    log_dir: Path


@dataclass(frozen=True)
class Limits:
    decode_workers: int
    encode_workers: int


@dataclass(frozen=True)
class ShowConfig:
    show: Show
    tools: Tools
    framing: FramingConfig
    colour: Colour
    decode: Decode
    sound: Sound
    scan: Scan
    deliverables: dict[str, Deliverable]
    burn_in: BurnIn
    naming: Naming
    paths: Paths
    limits: Limits
    config_hash: str = ""

    def decoded_size(self, camera: str, width: int, height: int) -> tuple[int, int]:
        """R3D is debayered at the configured fraction. ProRes is decoded at the size it was recorded."""
        resolution = self.decode.resolution if self.decode.sources.get(camera, "r3d") == "r3d" else "full"
        return decode_size(width, height, resolution)

    def nominal_framing(self, deliverable: str, camera: str) -> Framing:
        """Framing for a camera's expected resolution. Real clips are framed from their own metadata."""
        target = self.deliverables[deliverable]
        return compute_framing(
            self.decoded_size(camera, *self.decode.expected_resolution[camera]),
            crop_percent=self.framing.crop_percent,
            output=(target.width, target.height),
            fit=self.framing.fit,
            aspect_tolerance=self.framing.aspect_tolerance,
        )


# ------------------------------------------------------------------ reader

_MISSING = object()


class _Reader:
    """Typed access to one mapping, recording problems instead of raising."""

    def __init__(self, data: Any, path: str, problems: list[str], check_paths: bool):
        self.path = path
        self.problems = problems
        self.check_paths = check_paths
        self.seen: set[str] = set()
        if isinstance(data, dict):
            self.data = data
        else:
            self.data = {}
            if data is not _MISSING:
                self.fail(None, "expected a mapping")

    def key(self, name: str | None) -> str:
        return ".".join(part for part in (self.path, name) if part)

    def fail(self, name: str | None, message: str) -> None:
        self.problems.append(f"{self.key(name)}: {message}")

    def raw(self, name: str, *, required: bool = True) -> Any:
        self.seen.add(name)
        if name not in self.data:
            if required:
                self.fail(name, "missing")
            return _MISSING
        return self.data[name]

    def section(self, name: str) -> "_Reader":
        return _Reader(self.raw(name), self.key(name), self.problems, self.check_paths)

    def text(self, name: str, *, choices: tuple[str, ...] | None = None, required: bool = True) -> str | None:
        value = self.raw(name, required=required)
        if value is _MISSING:
            return None
        if not isinstance(value, str) or not value.strip():
            self.fail(name, "expected a non-empty string")
            return None
        if choices is not None and value not in choices:
            self.fail(name, f"{value!r} is not one of {list(choices)}")
            return None
        return value

    def integer(self, name: str, *, minimum: int | None = None, maximum: int | None = None,
                required: bool = True) -> int | None:
        value = self.raw(name, required=required)
        if value is _MISSING:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(name, f"expected an integer, got {value!r}")
            return None
        if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
            self.fail(name, f"{value} is outside {minimum} to {maximum}")
            return None
        return value

    def number(self, name: str, *, above: float | None = None, below: float | None = None,
               minimum: float | None = None) -> float | None:
        value = self.raw(name)
        if value is _MISSING:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.fail(name, f"expected a number, got {value!r}")
            return None
        value = float(value)
        if (above is not None and value <= above) or (below is not None and value >= below) or (
            minimum is not None and value < minimum
        ):
            self.fail(name, f"{value} is out of range")
            return None
        return value

    def boolean(self, name: str) -> bool | None:
        value = self.raw(name)
        if value is _MISSING:
            return None
        if not isinstance(value, bool):
            self.fail(name, f"expected true or false, got {value!r}")
            return None
        return value

    def items(self, name: str) -> list | None:
        value = self.raw(name)
        if value is _MISSING:
            return None
        if not isinstance(value, list):
            self.fail(name, "expected a list")
            return None
        return value

    def file(self, name: str) -> Path | None:
        return self._path(name, self.text(name), want="file")

    def folder(self, name: str, *, writable: bool) -> Path | None:
        return self._path(name, self.text(name), want="folder", writable=writable)

    def _path(self, name: str, value: str | None, *, want: str, writable: bool = False,
              executable: bool = False) -> Path | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute():
            self.fail(name, f"{value!r} must be an absolute path")
            return None
        if not self.check_paths:
            return path
        if want == "file" and not path.is_file():
            self.fail(name, f"file not found: {path}")
        elif want == "folder" and not path.is_dir():
            self.fail(name, f"folder not found: {path}")
        elif writable and not os.access(path, os.W_OK):
            self.fail(name, f"folder is not writable: {path}")
        elif executable and not os.access(path, os.X_OK):
            self.fail(name, f"file is not executable: {path}")
        return path

    def template(self, name: str, allowed: frozenset[str]) -> tuple[str, tuple[str, ...]] | None:
        value = self.text(name)
        if value is None:
            return None
        fields = _placeholders(value)
        if fields is None:
            self.fail(name, f"malformed template {value!r}")
            return None
        unknown = sorted(set(fields) - allowed)
        if unknown:
            self.fail(name, f"unknown placeholder(s) {unknown}, allowed: {sorted(allowed)}")
            return None
        return value, fields

    def finish(self) -> None:
        for name in self.data:
            if name not in self.seen:
                self.fail(str(name), "unknown key")


def _placeholders(template: str) -> tuple[str, ...] | None:
    try:
        return tuple(field for _, field, _, _ in string.Formatter().parse(template) if field)
    except ValueError:
        return None


# ----------------------------------------------------------------- parsing


def _show(r: _Reader) -> Show | None:
    name = r.text("name")
    code = r.text("code")
    if code is not None and not re.fullmatch(r"[A-Za-z0-9]+", code):
        r.fail("code", f"{code!r} must be letters and digits only, it is used in folder names")
        code = None
    fps = r.integer("fps")
    if fps is not None and fps != SHOW_FPS:
        r.fail("fps", f"this pipeline is built for {SHOW_FPS} fps with no rate conversion, got {fps}")
        fps = None
    audio_rate = r.integer("audio_rate", minimum=1)
    if audio_rate is not None and audio_rate % SHOW_FPS:
        r.fail("audio_rate", f"{audio_rate} does not give a whole number of samples per frame at {SHOW_FPS} fps")
        audio_rate = None
    timecode = r.text("timecode", choices=("non_drop",))
    cameras = _labels(r, "cameras", r"[A-Z]", "single capital letters")
    units = _labels(r, "units", r"[A-Za-z0-9]+", "letters and digits")
    r.finish()
    if None in (name, code, fps, audio_rate, timecode, cameras, units):
        return None
    return Show(name, code, fps, audio_rate, timecode, cameras, units)


def _labels(r: _Reader, name: str, pattern: str, wording: str) -> tuple[str, ...] | None:
    values = r.items(name)
    if values is None:
        return None
    if not values:
        r.fail(name, "must list at least one entry")
        return None
    if not all(isinstance(v, str) and re.fullmatch(pattern, v) for v in values):
        r.fail(name, f"entries must be {wording}, got {values!r}")
        return None
    if len(set(values)) != len(values):
        r.fail(name, f"entries must be unique, got {values!r}")
        return None
    return tuple(values)


def _tools(r: _Reader, engine: str | None) -> Tools | None:
    parsed: dict[str, Tool | None] = {}
    ok = True
    for name in ("ffmpeg", "ffprobe", "redline", "r3dtool"):
        optional = name in ENGINE_TOOL.values()
        if optional and name not in r.data:
            r.seen.add(name)
            parsed[name] = None
            if engine is not None and ENGINE_TOOL[engine] == name:
                r.fail(name, f"missing, required by decode.engine {engine}")
                ok = False
            continue
        t = r.section(name)
        path = t._path("path", t.text("path"), want="file", executable=True)
        version = t.text("version")
        t.finish()
        parsed[name] = Tool(path, version) if path is not None and version is not None else None
        ok = ok and parsed[name] is not None
    r.finish()
    return Tools(**parsed) if ok else None  # type: ignore[arg-type]


def _framing(r: _Reader) -> FramingConfig | None:
    crop = r.number("crop_percent", minimum=0.0, below=100.0)
    order = r.text("crop_order", choices=("crop_then_scale",))
    fit = r.text("fit", choices=FIT_MODES)
    tolerance = r.number("aspect_tolerance", above=0.0, below=0.05)
    r.finish()
    if None in (crop, order, fit, tolerance):
        return None
    return FramingConfig(crop, order, fit, tolerance)


def _colour(r: _Reader, cameras: tuple[str, ...] | None) -> Colour | None:
    ocio = r.file("ocio_config")
    decode_space = r.text("decode_space")
    transform = r.text("output_transform")
    show_lut = r.file("show_lut")
    looks_reader = r.section("camera_looks")
    looks: dict[str, Path | None] = {}
    looks_ok = True
    for camera in cameras or ():
        value = looks_reader.raw(camera, required=False)
        if value is _MISSING:
            looks_reader.fail(camera, "missing, use null if this camera deliberately has no look")
            looks_ok = False
        elif value is None:
            looks[camera] = None
        else:
            path = looks_reader.file(camera)
            looks[camera] = path
            looks_ok = looks_ok and path is not None
    if cameras is not None:
        for extra in looks_reader.data:
            if extra not in cameras:
                looks_reader.seen.add(extra)
                looks_reader.fail(str(extra), "camera is not listed in show.cameras")
                looks_ok = False
    cdl_dir = r.folder("cdl_dir", writable=False)
    lut_cache = r.folder("lut_cache", writable=True)
    bake_size = r.integer("bake_size", minimum=17, maximum=129)
    clamp = r.boolean("cdl_clamp")
    apply = r.text("apply", choices=("exact", "baked"))
    search = r.items("cdl_search")
    if search is not None:
        bad = not search or not all(isinstance(t, str) and t.strip() for t in search)
        if not bad:
            for template in search:
                fields = _placeholders(template)
                unknown = sorted(set(fields or ()) - CDL_SEARCH_PLACEHOLDERS)
                if fields is None or unknown:
                    r.fail("cdl_search", f"{template!r} uses unknown placeholder(s) {unknown}, allowed: "
                                         f"{sorted(CDL_SEARCH_PLACEHOLDERS)}")
                    search = None
                    break
                if Path(template).is_absolute() or ".." in Path(template).parts:
                    bad = True
        if bad:
            r.fail("cdl_search", "expected a non-empty list of relative templates that stay under cdl_dir")
            search = None
    default_ok = True
    default = r.raw("cdl_default")
    if default is _MISSING:
        default_ok = False
    elif default is not None and (not isinstance(default, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", default)):
        r.fail("cdl_default", f"{default!r} must be a plain name such as 000, without folder or extension, or null")
        default_ok = False
    r.finish()
    if None in (ocio, decode_space, transform, show_lut, cdl_dir, lut_cache, bake_size, clamp, apply, search) \
            or not looks_ok or not default_ok:
        return None
    return Colour(ocio, decode_space, None if transform == "none" else transform, show_lut, looks,
                  cdl_dir, lut_cache, bake_size, clamp, apply, tuple(search), default)


def _args(r: _Reader, name: str, *, must_include: str) -> tuple[str, ...] | None:
    values = r.items(name)
    if values is None:
        return None
    if not values or not all(isinstance(v, str) for v in values):
        r.fail(name, "expected a non-empty list of strings")
        return None
    used: set[str] = set()
    for value in values:
        fields = _placeholders(value)
        if fields is None:
            r.fail(name, f"malformed template {value!r}")
            return None
        used.update(fields)
    unknown = sorted(used - REDLINE_PLACEHOLDERS)
    if unknown:
        r.fail(name, f"unknown placeholder(s) {unknown}, allowed: {sorted(REDLINE_PLACEHOLDERS)}")
        return None
    if must_include not in used:
        r.fail(name, f"must reference {{{must_include}}}")
        return None
    return tuple(values)


def _per_camera(r: _Reader, name: str, cameras: tuple[str, ...] | None, parse) -> dict | None:
    """A mapping with exactly one entry per show camera."""
    section = r.section(name)
    if cameras is None:
        section.seen.update(section.data)
        return None
    values: dict = {}
    ok = True
    for camera in cameras:
        raw = section.raw(camera)
        parsed = parse(section, camera, raw) if raw is not _MISSING else None
        ok = ok and parsed is not None
        values[camera] = parsed
    for extra in section.data:
        if extra not in cameras:
            section.seen.add(extra)
            section.fail(str(extra), "camera is not listed in show.cameras")
            ok = False
    return values if ok else None


def _resolution(section: _Reader, camera: str, raw: Any) -> tuple[int, int] | None:
    if not (isinstance(raw, list) and len(raw) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in raw)):
        section.fail(camera, f"expected [width, height] as positive integers, got {raw!r}")
        return None
    return (raw[0], raw[1])


def _source_kind(section: _Reader, camera: str, raw: Any) -> str | None:
    if raw not in ("r3d", "prores"):
        section.fail(camera, f"{raw!r} is not one of ['r3d', 'prores']")
        return None
    return raw


def _decode(r: _Reader, cameras: tuple[str, ...] | None) -> Decode | None:
    engine = r.text("engine", choices=("redline", "sdk"))
    resolution = r.text("resolution", choices=tuple(DECODE_DIVISORS))
    bit_depth = r.integer("bit_depth", minimum=16, maximum=16)
    transport = r.text("transport", choices=("file", "pipe"))
    sources = _per_camera(r, "sources", cameras, _source_kind)
    expected = _per_camera(r, "expected_resolution", cameras, _resolution)
    letter_from = r.text("camera_letter_from", choices=("reel_prefix", "camera_id"))
    if engine == "sdk" and transport == "file":
        r.fail("transport", "the sdk engine writes frames to a pipe, set transport to pipe")
        transport = None
    redline: RedlineConfig | None = None
    redline_ok = True
    if "redline" in r.data:
        red = r.section("redline")
        metadata_args = _args(red, "metadata_args", must_include="clip")
        decode_args = _args(red, "decode_args", must_include="clip")
        pattern = red.template("intermediate_pattern", frozenset({"out_name"}))
        start = red.integer("intermediate_start_number", minimum=0)
        red.finish()
        if None in (metadata_args, decode_args, pattern, start):
            redline_ok = False
        else:
            redline = RedlineConfig(metadata_args, decode_args, pattern[0], start)
    elif engine == "redline":
        r.seen.add("redline")
        r.fail("redline", "missing, required by decode.engine redline")
        redline_ok = False
    r.finish()
    if None in (engine, resolution, bit_depth, transport, sources, expected, letter_from) or not redline_ok:
        return None
    return Decode(engine, resolution, bit_depth, transport, sources, expected, letter_from, redline)


def _sound(r: _Reader, cameras: tuple[str, ...] | None) -> Sound | None:
    tracks = r.items("mix_tracks")
    if tracks is not None:
        valid = tracks and all(isinstance(t, int) and not isinstance(t, bool) and t >= 1 for t in tracks)
        if not valid or len(set(tracks)) != len(tracks):
            r.fail("mix_tracks", f"expected unique track numbers counting from 1, got {tracks!r}")
            tracks = None
    window = r.number("tc_window_hours", above=0.0, below=12.0)
    pattern_ok = True
    pattern = r.raw("filename_pattern")
    if pattern is _MISSING:
        pattern_ok = False
    elif pattern is not None:
        try:
            groups = re.compile(pattern).groupindex if isinstance(pattern, str) else None
        except re.error as error:
            groups = None
            r.fail("filename_pattern", f"not a valid regular expression: {error}")
            pattern_ok = False
        else:
            if groups is None or not {"scene", "take"} <= set(groups):
                r.fail("filename_pattern", "must be a regular expression with named groups scene and take, or null")
                pattern_ok = False
    mos = r.items("mos_cameras")
    if mos is not None and cameras is not None:
        unknown = [c for c in mos if c not in cameras]
        if unknown or len(set(map(str, mos))) != len(mos):
            r.fail("mos_cameras", f"entries must be unique cameras from show.cameras, got {mos!r}")
            mos = None
    r.finish()
    if tracks is None or window is None or mos is None or not pattern_ok:
        return None
    return Sound(tuple(tracks), window, pattern, tuple(mos))


def _scan(r: _Reader) -> Scan | None:
    values: dict[str, str | None] = {}
    for name in ("camera_dir", "sound_dir"):
        parsed = r.template(name, SCAN_DIR_PLACEHOLDERS)
        value = parsed[0] if parsed else None
        if value is not None and (Path(value).is_absolute() or ".." in Path(value).parts):
            r.fail(name, f"{value!r} must be a relative path that stays under its root")
            value = None
        values[name] = value
    r.finish()
    if None in values.values():
        return None
    return Scan(**values)  # type: ignore[arg-type]


def _deliverable(r: _Reader, name: str) -> Deliverable | None:
    codec = r.text("codec", choices=tuple(CODEC_RULES))
    container = r.text("container")
    pix_fmt = r.text("pix_fmt")
    if codec is not None:
        rules = CODEC_RULES[codec]
        if container is not None and container not in rules["containers"]:
            r.fail("container", f"{container!r} cannot carry {codec}, expected one of {list(rules['containers'])}")
            container = None
        if pix_fmt is not None and pix_fmt not in rules["pix_fmts"]:
            r.fail("pix_fmt", f"{pix_fmt!r} is not valid for {codec}, expected one of {list(rules['pix_fmts'])}")
            pix_fmt = None
    dims: dict[str, int | None] = {}
    for axis in ("width", "height"):
        value = r.integer(axis, minimum=2)
        if value is not None and value % 2:
            r.fail(axis, f"{value} must be even")
            value = None
        dims[axis] = value
    crf: int | None = None
    crf_ok = True
    if codec == "h264":
        crf = r.integer("crf", minimum=0, maximum=51)
        crf_ok = crf is not None
    elif "crf" in r.data:
        r.seen.add("crf")
        r.fail("crf", f"only h264 takes a crf, codec is {codec}")
        crf_ok = False
    audio = r.text("audio")
    audio_codec: str | None = None
    bitrate: int | None = None
    if audio is not None:
        aac = _AAC.match(audio)
        if audio in PCM_CODECS:
            audio_codec = audio
        elif aac and int(aac.group(1)) > 0:
            audio_codec, bitrate = "aac", int(aac.group(1))
        else:
            r.fail("audio", f"{audio!r} is not one of {list(PCM_CODECS)} or aac_<kbps>k")
    burn_in = r.boolean("burn_in")
    r.finish()
    if None in (codec, container, pix_fmt, dims["width"], dims["height"], audio_codec, burn_in) or not crf_ok:
        return None
    return Deliverable(name, codec, container, dims["width"], dims["height"], pix_fmt, crf, audio_codec,
                       bitrate, burn_in)


def _deliverables(r: _Reader) -> dict[str, Deliverable] | None:
    if not r.data:
        if not any(p.startswith(r.path + ":") for p in r.problems):
            r.fail(None, "at least one deliverable is required")
        return None
    parsed = {name: _deliverable(r.section(name), name) for name in list(r.data)}
    if None in parsed.values():
        return None
    if len({(d.width, d.height) for d in parsed.values()}) > 1:
        # one decode feeds one scaler, then the stream is split per deliverable
        r.fail(None, "all deliverables must have the same width and height in this build")
        return None
    return parsed  # type: ignore[return-value]


def _burn_in(r: _Reader) -> BurnIn | None:
    font = r.file("font")
    size = r.number("size_ratio", above=0.0, below=0.2)
    margin = r.number("margin_ratio", minimum=0.0, below=0.2)
    fields_reader = r.section("fields")
    fields: dict[str, str] = {}
    fields_ok = True
    for position in list(fields_reader.data):
        if position not in BURN_IN_POSITIONS:
            fields_reader.seen.add(position)
            fields_reader.fail(str(position), f"unknown position, expected one of {list(BURN_IN_POSITIONS)}")
            fields_ok = False
            continue
        parsed = fields_reader.template(position, BURN_IN_PLACEHOLDERS)
        if parsed is None:
            fields_ok = False
            continue
        template, used = parsed
        if RUNNING_TC in used and (used.count(RUNNING_TC) > 1 or not template.endswith("{" + RUNNING_TC + "}")):
            fields_reader.fail(position, f"{{{RUNNING_TC}}} is a running timecode and must come once, at the end")
            fields_ok = False
            continue
        fields[position] = template
    r.finish()
    if None in (font, size, margin) or not fields_ok:
        return None
    return BurnIn(font, size, margin, fields)


def _naming(r: _Reader) -> Naming | None:
    day_folder = r.template("day_folder", NAMING_PLACEHOLDERS)
    tape = r.template("tape", NAMING_PLACEHOLDERS - SLATE_PLACEHOLDERS)
    avid_clip = r.template("avid_clip", NAMING_PLACEHOLDERS)
    fallback = r.template("avid_clip_fallback", NAMING_PLACEHOLDERS - SLATE_PLACEHOLDERS)
    r.finish()
    if None in (day_folder, tape, avid_clip, fallback):
        return None
    return Naming(day_folder[0], tape[0], avid_clip[0], fallback[0])


def _paths(r: _Reader) -> Paths | None:
    values = {
        "camera_root": r.folder("camera_root", writable=False),
        "sound_root": r.folder("sound_root", writable=False),
        "output_root": r.folder("output_root", writable=True),
        "scratch": r.folder("scratch", writable=True),
        "manifest_dir": r.folder("manifest_dir", writable=True),
        "log_dir": r.folder("log_dir", writable=True),
    }
    r.finish()
    if None in values.values():
        return None
    return Paths(**values)  # type: ignore[arg-type]


def _limits(r: _Reader) -> Limits | None:
    decode_workers = r.integer("decode_workers", minimum=1)
    encode_workers = r.integer("encode_workers", minimum=1)
    r.finish()
    if decode_workers is None or encode_workers is None:
        return None
    return Limits(decode_workers, encode_workers)


def _check_framing(problems: list[str], framing: FramingConfig, decode: Decode,
                   deliverables: dict[str, Deliverable]) -> None:
    for name, target in deliverables.items():
        for camera, recorded in decode.expected_resolution.items():
            source = decode_size(*recorded, decode.resolution if decode.sources[camera] == "r3d" else "full")
            try:
                compute_framing(source, crop_percent=framing.crop_percent, output=(target.width, target.height),
                                fit=framing.fit, aspect_tolerance=framing.aspect_tolerance)
            except FramingError as error:
                problems.append(f"deliverables.{name}: camera {camera}: {error}")


# ------------------------------------------------------------------ public


def _canonical(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _canonical(getattr(value, f.name)) for f in dataclasses.fields(value) if f.name != "config_hash"}
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def hash_of(value: Any) -> str:
    """sha256 over canonical JSON. Key order, YAML comments and int-vs-float spelling do not matter."""
    blob = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def load_config(path: Path | str, *, check_paths: bool = True) -> ShowConfig:
    """Load and fully validate a show config. Raises ConfigError listing every problem."""
    path = Path(path)
    try:
        text = path.read_text()
    except OSError as error:
        raise ConfigError([f"config file not found or unreadable: {error}"], path) from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigError([f"invalid YAML: {error}"], path) from None
    if not isinstance(data, dict):
        raise ConfigError(["top level of the config must be a mapping of sections"], path)

    problems: list[str] = []
    root = _Reader(data, "", problems, check_paths)
    show = _show(root.section("show"))
    engine = data.get("decode", {}).get("engine") if isinstance(data.get("decode"), dict) else None
    tools = _tools(root.section("tools"), engine if engine in ENGINE_TOOL else None)
    framing = _framing(root.section("framing"))
    colour = _colour(root.section("colour"), show.cameras if show else None)
    decode = _decode(root.section("decode"), show.cameras if show else None)
    sound = _sound(root.section("sound"), show.cameras if show else None)
    scan = _scan(root.section("scan"))
    deliverables = _deliverables(root.section("deliverables"))
    burn_in = _burn_in(root.section("burn_in"))
    naming = _naming(root.section("naming"))
    paths = _paths(root.section("paths"))
    limits = _limits(root.section("limits"))
    root.finish()

    if framing and decode and deliverables:
        _check_framing(problems, framing, decode, deliverables)
    if problems:
        raise ConfigError(problems, path)

    config = ShowConfig(show, tools, framing, colour, decode, sound, scan, deliverables, burn_in, naming, paths,
                        limits)
    return dataclasses.replace(config, config_hash=hash_of(config))
