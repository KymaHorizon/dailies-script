# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Sound file metadata.

ffprobe gives the BWF time_reference (samples since midnight), the stream facts
and the bext description. It does not expose the iXML chunk, which was checked
against FFmpeg 7.1.1, so iXML is read here with a small RIFF walker that also
understands RF64 and BW64 for files over 4 GB.

Scene, take and sound roll are resolved in this order: iXML, then the bext
description (the sSCENE=, sTAKE=, sTAPE= lines recorders write), then the
filename pattern from the config, then the parent folder name for the roll.

Everything stays in samples. Frames are for display only.
"""

from __future__ import annotations

import json
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from dailies.commands.base import Command
from dailies.config import ShowConfig

_BEXT_LINE = re.compile(r"^s(SCENE|TAKE|TAPE)=(.*)$", re.MULTILINE)
_MAX_IXML_BYTES = 8 * 1024 * 1024


class WavError(Exception):
    """This file cannot be used as a sound source."""


@dataclass(frozen=True)
class ProbeFacts:
    sample_rate: int
    channels: int
    bit_depth: int
    sample_count: int
    start_samples: int | None  # BWF time_reference, None when the file carries no timecode
    bext: dict[str, str]


def probe_command(config: ShowConfig, path: Path) -> Command:
    return Command((str(config.tools.ffprobe.path), "-v", "error", "-show_format", "-show_streams",
                    "-print_format", "json", str(path)))


def parse_probe(stdout: str) -> ProbeFacts:
    try:
        data = json.loads(stdout)
        stream = next(s for s in data["streams"] if s.get("codec_type") == "audio")
        tags = data["format"].get("tags", {})
        reference = tags.get("time_reference")
        bext = {key.lower(): value.strip() for key, value in _BEXT_LINE.findall(tags.get("comment", ""))
                if value.strip()}
        return ProbeFacts(
            sample_rate=int(stream["sample_rate"]),
            channels=int(stream["channels"]),
            bit_depth=int(stream.get("bits_per_sample") or stream.get("bits_per_raw_sample") or 0),
            sample_count=int(stream["duration_ts"]),
            start_samples=int(reference) if reference is not None else None,
            bext=bext,
        )
    except (ValueError, KeyError, StopIteration, TypeError, AttributeError) as error:
        raise WavError(f"unreadable ffprobe output: {type(error).__name__} {error}") from None


def _ixml_bytes(path: Path) -> bytes | None:
    with open(path, "rb") as handle:
        header = handle.read(12)
        if len(header) < 12 or header[:4] not in (b"RIFF", b"RF64", b"BW64") or header[8:12] != b"WAVE":
            raise WavError(f"{path.name} is not a RIFF WAVE file")
        data_size_64: int | None = None
        while True:
            head = handle.read(8)
            if len(head) < 8:
                return None
            tag, size = head[:4], struct.unpack("<I", head[4:])[0]
            if tag == b"ds64":
                body = handle.read(size + (size & 1))
                if len(body) >= 16:
                    data_size_64 = struct.unpack("<Q", body[8:16])[0]
                continue
            if tag == b"iXML":
                if size > _MAX_IXML_BYTES:
                    return None
                body = handle.read(size)
                return body if len(body) == size else None
            if tag == b"data" and size == 0xFFFFFFFF:
                if data_size_64 is None:
                    return None
                size = data_size_64
            handle.seek(size + (size & 1), 1)


def read_ixml(path: Path) -> dict:
    """Scene, take, tape and track names from the iXML chunk. Empty when absent or damaged."""
    raw = _ixml_bytes(Path(path))
    if raw is None:
        return {}
    try:
        root = ET.fromstring(raw.rstrip(b"\x00"))
    except ET.ParseError:
        return {}
    found: dict = {}
    for key in ("scene", "take", "tape"):
        text = (root.findtext(key.upper()) or "").strip()
        if text:
            found[key] = text
    tracks = []
    for track in root.findall("./TRACK_LIST/TRACK"):
        index = (track.findtext("INTERLEAVE_INDEX") or track.findtext("CHANNEL_INDEX") or "").strip()
        tracks.append((int(index) if index.isdigit() else len(tracks) + 1, (track.findtext("NAME") or "").strip()))
    if tracks:
        found["track_names"] = [name for _, name in sorted(tracks)]
    return found


def sound_file_entry(config: ShowConfig, path: Path, probe: ProbeFacts, ixml: dict) -> dict:
    """One entry of the manifest's sound file inventory."""
    path = Path(path)
    by_name: dict[str, str] = {}
    if config.sound.filename_pattern:
        match = re.search(config.sound.filename_pattern, path.stem)
        if match:
            by_name = {key: value for key, value in match.groupdict().items() if value}
    source = "none"
    for name, candidate in (("ixml", ixml), ("bext", probe.bext), ("filename", by_name)):
        if candidate.get("scene") or candidate.get("take"):
            source = name
            break
    chosen = {"ixml": ixml, "bext": probe.bext, "filename": by_name}.get(source, {})
    flags = []
    if probe.start_samples is None:
        flags.append("no_time_reference")
    if probe.sample_rate != config.show.audio_rate:
        flags.append("wrong_sample_rate")
    start = probe.start_samples
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "soundroll": ixml.get("tape") or probe.bext.get("tape") or path.parent.name,
        "scene": chosen.get("scene"),
        "take": chosen.get("take"),
        "metadata_source": source,
        "track_names": ixml.get("track_names", []),
        "sample_rate": probe.sample_rate,
        "channels": probe.channels,
        "bit_depth": probe.bit_depth,
        "sample_count": probe.sample_count,
        "start_samples": start,
        "end_samples": start + probe.sample_count if start is not None else None,
        "flags": flags,
    }
