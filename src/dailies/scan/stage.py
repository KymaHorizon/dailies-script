# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Scan stage: build or refresh the day manifest from sealed offloads.

This is the only stage that looks at the filesystem. Everything it learns goes
into the manifest, and no later stage looks at the disk to decide anything.

Order of work:
1. Check every pinned tool reports its pinned version. A mismatch stops the run.
2. Gate each camera and sound folder on its ASC MHL. Unsealed folders are
   skipped with a reason and the rest of the day continues.
3. One decoder call per logical clip, first span file only.
4. One ffprobe call plus an iXML read per sound file.

Anything unexpected about a clip is flagged and reported. Nothing is adapted to.
A clip the decoder cannot read is recorded as a scan error and the batch goes
on. Two clips with the same name from different folders stop the run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from dailies.commands.base import Command, Executor
from dailies.config import ShowConfig
import dataclasses

from dailies.decoder import ClipFacts, Decoder, DecoderError, decoder_for, make_decoder
from dailies.manifest import Manifest, ManifestError
from dailies.naming import DayContext, day_folder
from dailies.scan.clips import SourceClip, discover_clips
from dailies.scan.mhl import check_mhl
from dailies.scan.wav import WavError, parse_probe, probe_command, read_ixml, sound_file_entry
from dailies.timecode import TimecodeError, frames_to_tc, tc_to_frames

_FF_VERSION = re.compile(r"^ff(?:mpeg|probe) version (\S+)")
SCAN_FLAGS = {"wrong_fps", "unexpected_resolution", "edge_timecode", "short_clip", "dropped_frames", "anamorphic",
              "unknown_camera", "unexpected_source"}
SOURCE_KINDS = {"r3d": "r3d", "mov": "prores"}  # file kind on disk -> what decode.sources calls it


class ScanAbort(Exception):
    """The run cannot continue. Exit code 2."""


@dataclass
class ScanSummary:
    clips_scanned: int = 0
    clips_failed: int = 0
    sound_files: int = 0
    folders_skipped: int = 0
    warnings: list[str] = field(default_factory=list)


def _day_dir(root: Path, template: str, config: ShowConfig, day: DayContext) -> Path:
    return root / template.format(show=day.show, date=day.date, day_code=day.day_code, unit=day.unit,
                                  day_folder=day_folder(config.naming, day))


def _units(folder: Path) -> list[Path]:
    """Folders to gate. A day sealed as one history is one unit, otherwise each roll folder is."""
    if (folder / "ascmhl").is_dir():
        return [folder]
    return sorted(p for p in folder.iterdir() if p.is_dir())


def _check_tools(config: ShowConfig, decoder: Decoder, executor: Executor) -> dict | None:
    """Run every pinned tool's version command. Returns None on a dry run."""
    checks: list[tuple[str, Path, str, Command, object]] = [
        ("ffmpeg", config.tools.ffmpeg.path, config.tools.ffmpeg.version,
         Command((str(config.tools.ffmpeg.path), "-version")), None),
        ("ffprobe", config.tools.ffprobe.path, config.tools.ffprobe.version,
         Command((str(config.tools.ffprobe.path), "-version")), None),
    ]
    decoder_tool = config.tools.r3dtool if config.decode.engine == "sdk" else config.tools.redline
    decoder_name = "r3dtool" if config.decode.engine == "sdk" else "redline"
    checks.append((decoder_name, decoder_tool.path, decoder_tool.version, decoder.version_command(), decoder))

    recorded: dict = {}
    dry = False
    for name, path, pinned, command, parser in checks:
        result = executor.run(command, clip="-", stage="scan")
        if result is None:
            dry = True
            continue
        if not result.ok:
            raise ScanAbort(f"tools.{name}: {path} failed to report its version: {result.stderr.strip()}")
        try:
            if parser is not None:
                reported = parser.parse_version(result.stdout)
            else:
                reported = _FF_VERSION.match(result.stdout).group(1)  # type: ignore[union-attr]
        except (DecoderError, AttributeError) as error:
            raise ScanAbort(f"tools.{name}: unreadable version output from {path}: {error}") from None
        if not reported.startswith(pinned):
            raise ScanAbort(f"tools.{name}: the config pins version {pinned}, {path} reports {reported}. "
                            "A tool change mid-shoot must be deliberate: update the config to accept it.")
        recorded[name] = {"path": str(path), "version": reported}
    return None if dry else recorded


def _picture(config: ShowConfig, facts: ClipFacts, kind: str = "r3d") -> tuple[dict, list[str]]:
    rate = max(1, round(facts.fps))
    start_frames = tc_to_frames(facts.start_tc, fps=rate)
    camera = facts.camera_id if config.decode.camera_letter_from == "camera_id" else facts.clip_name[:1]
    picture = {
        "camera": camera, "roll": facts.roll, "clip": facts.clip, "reel": f"{camera}{facts.roll}",
        "width": facts.width, "height": facts.height, "fps": facts.fps, "frames": facts.frames,
        "start_tc": facts.start_tc,
        "end_tc": frames_to_tc(start_frames + facts.frames, fps=rate),  # exclusive: start plus duration
        "anamorphic": facts.anamorphic, "iso": facts.iso, "kelvin": facts.kelvin, "tint": facts.tint,
        "scene": facts.scene, "take": facts.take,
        "tc_absolute": facts.tc_absolute, "tc_edge": facts.tc_edge, "tc_preference": facts.tc_preference,
        "camera_model": facts.camera_model, "dropped_frames": facts.dropped_frames, "codec": facts.codec,
    }
    flags = []
    if facts.fps != config.show.fps:
        flags.append("wrong_fps")
    expected = config.decode.expected_resolution.get(camera)
    if expected is not None and (facts.width, facts.height) != expected:
        flags.append("unexpected_resolution")
    if facts.tc_preference == "edge":
        flags.append("edge_timecode")
    if facts.frames < rate:
        flags.append("short_clip")
    if facts.dropped_frames:
        flags.append("dropped_frames")
    if facts.anamorphic != 1.0:
        flags.append("anamorphic")
    if camera not in config.show.cameras:
        flags.append("unknown_camera")
    elif config.decode.sources[camera] != SOURCE_KINDS[kind]:
        flags.append("unexpected_source")
    return picture, flags


def _join_parts(parts: list[ClipFacts]) -> ClipFacts:
    """A spanned movie is several files that are each a complete movie. Their frames add up, and each part
    must start on the timecode where the one before it ended, or something is missing in between."""
    first = parts[0]
    rate = max(1, round(first.fps))
    expected = tc_to_frames(first.start_tc, fps=rate) + first.frames
    for number, part in enumerate(parts[1:], start=2):
        if tc_to_frames(part.start_tc, fps=rate) != expected:
            raise DecoderError(f"span part {number:03d} starts at {part.start_tc}, which does not continue from the "
                               f"part before it (expected {frames_to_tc(expected, fps=rate)})")
        expected += part.frames
    return dataclasses.replace(first, frames=sum(part.frames for part in parts))


def _scan_clip(config: ShowConfig, manifest: Manifest, decoder: Decoder, executor: Executor, source: SourceClip,
               summary: ScanSummary, errors: list[dict]) -> None:
    def fail(message: str, stderr: str = "") -> None:
        summary.clips_failed += 1
        errors.append({"clip": source.clip_name, "message": message, "stderr": stderr})

    if source.gaps:
        missing = ", ".join(f"{n:03d}" for n in source.gaps)
        return fail(f"span part {missing} is missing, the clip was not scanned")
    # R3D: only the first file is addressed, the decoder follows the span. A spanned movie is probed part by part.
    decoder = decoder_for(config, source.first) if source.kind == "mov" else decoder
    probed: list[ClipFacts] = []
    stderr = ""
    for part in (source.parts if source.kind == "mov" else (source.first,)):
        result = executor.run(decoder.metadata_command(part), clip=source.clip_name, stage="scan")
        if result is None:
            return None  # dry run
        if not result.ok:
            return fail(f"{decoder.name} decoder exited {result.returncode}", result.stderr)
        stderr = result.stderr
        try:
            probed.append(decoder.parse_metadata(result.stdout))
        except DecoderError as error:
            return fail(str(error), result.stderr)
    try:
        facts = _join_parts(probed)
        picture, flags = _picture(config, facts, source.kind)
    except (DecoderError, TimecodeError) as error:
        return fail(str(error), stderr)
    try:
        manifest.add_clip(facts.clip_name, mob_id=None, picture=picture,
                          source={"path": str(source.first), "size_bytes": source.size_bytes, "mhl_verified": True,
                                  "kind": source.kind, "parts": [str(part) for part in source.parts]})
    except ManifestError as error:
        # the blocks were built here, so the only way in is a clip id collision, and that stops the run
        raise ScanAbort(str(error)) from None
    manifest.replace_flags(facts.clip_name, owned=SCAN_FLAGS, current=flags)
    summary.warnings += [f"{facts.clip_name}: {flag}" for flag in flags]
    summary.clips_scanned += 1
    return None


def run_scan(config: ShowConfig, manifest: Manifest, day: DayContext, executor: Executor,
             decoder: Decoder | None = None) -> ScanSummary:
    decoder = decoder or make_decoder(config)
    summary = ScanSummary()
    folders: list[dict] = []
    errors: list[dict] = []

    tools = _check_tools(config, decoder, executor)

    camera_dir = _day_dir(config.paths.camera_root, config.scan.camera_dir, config, day)
    if not camera_dir.is_dir():
        raise ScanAbort(f"camera folder for {day.day} not found: {camera_dir}")
    for unit in _units(camera_dir):
        gate = check_mhl(unit)
        folders.append({"path": str(unit), "kind": "camera", "eligible": gate.eligible, "reason": gate.reason})
        if not gate.eligible:
            summary.folders_skipped += 1
            summary.warnings.append(f"skipped {unit.name}: {gate.reason}")
            continue
        for source in discover_clips(unit):
            _scan_clip(config, manifest, decoder, executor, source, summary, errors)

    sound_entries: list[dict] = []
    sound_dir = _day_dir(config.paths.sound_root, config.scan.sound_dir, config, day)
    if not sound_dir.is_dir():
        summary.warnings.append(f"no sound folder for {day.day}: {sound_dir}")
    else:
        for unit in _units(sound_dir):
            gate = check_mhl(unit)
            folders.append({"path": str(unit), "kind": "sound", "eligible": gate.eligible, "reason": gate.reason})
            if not gate.eligible:
                summary.folders_skipped += 1
                summary.warnings.append(f"skipped {unit.name}: {gate.reason}")
                continue
            wavs = sorted(p for p in unit.rglob("*") if p.is_file() and p.suffix.lower() == ".wav"
                          and not p.name.startswith("."))
            for path in wavs:
                result = executor.run(probe_command(config, path), clip=path.name, stage="scan")
                if result is None:
                    continue  # dry run
                try:
                    if not result.ok:
                        raise WavError(f"ffprobe exited {result.returncode}")
                    entry = sound_file_entry(config, path, parse_probe(result.stdout), read_ixml(path))
                except WavError as error:
                    summary.clips_failed += 1
                    errors.append({"clip": path.name, "message": str(error), "stderr": result.stderr})
                    continue
                for flag in entry["flags"]:
                    summary.warnings.append(f"{path.name}: {flag}")
                sound_entries.append({**entry, "mhl_verified": True})

    if tools is None:
        return summary  # dry run: commands were printed, the manifest is untouched
    manifest.set_tools(tools)
    manifest.set_sound_files(sound_entries)
    manifest.set_scan_report(folders=folders, errors=errors)
    summary.sound_files = len(sound_entries)
    return summary
