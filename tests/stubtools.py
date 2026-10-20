# SPDX-FileCopyrightText: 2026 Kyma Horizon Ltd
# SPDX-License-Identifier: Apache-2.0

"""Stub command-line tools and media builders for scan tests. Test-only code.

The stubs are real executables run through the real executor. r3dtool answers from the clip file name,
with an optional .facts.json beside the clip to override values. Rolls are sealed with the real ascmhl tool.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

ASCMHL = Path(sys.executable).with_name("ascmhl")

R3DTOOL = '''#!{python}
import json, pathlib, re, sys
if sys.argv[1] == "version":
    print(json.dumps({{"tool": "r3dtool", "tool_version": "0.1.0", "sdk": "R3DAPI 9.2.1-test (x) [/x/]"}}))
    sys.exit(0)
path = pathlib.Path(sys.argv[2])
name = re.sub(r"_\\d{{3}}\\.R3D$", "", path.name)
if "BROKEN" in name:
    sys.stderr.write("r3dtool: failed to load clip, status 3\\n")
    sys.exit(4)
cam, roll, clip = re.match(r"([A-Z])(\\d{{3}})_C(\\d{{3}})", name).groups()
data = {{"path": str(path), "width": 8192, "height": 4320, "frames": 1732, "fps": 50, "audio_channels": 0,
        "tc_default": ["10:14:22:31", "10:14:57:12"], "tc_absolute": ["10:14:22:31", "10:14:57:12"],
        "tc_edge": ["01:00:00:00", "01:00:34:31"],
        "metadata": {{"reel_id_full": name, "camera_id": cam, "reel_id": roll, "clip_id": clip,
                     "framerate_numerator": 50, "framerate_denominator": 1, "user_timecode_preference": 1,
                     "pixel_aspect_ratio": 1, "iso": 800, "white_balance_kelvin": 5600, "white_balance_tint": 0,
                     "dropped_frame_count": 0, "camera_model": "V-RAPTOR 8K VV"}}}}
override = path.parent / (name + ".facts.json")
if override.exists():
    extra = json.loads(override.read_text())
    data["metadata"].update(extra.pop("metadata", {{}}))
    data.update(extra)
print(json.dumps(data))
'''

FFPROBE = '''#!{python}
import json, pathlib, re, sys
if "-version" in sys.argv:
    print("ffprobe version 7.1.1 Copyright (c) 2007-2025 the FFmpeg developers")
    sys.exit(0)
path = pathlib.Path(sys.argv[-1])
if "BROKEN" in path.name:
    sys.stderr.write("Invalid data found when processing input\\n")
    sys.exit(1)
if path.suffix.lower() == ".mov":
    video = {{"codec_type": "video", "codec_name": "prores", "profile": "HQ", "width": 4096, "height": 2160,
             "sample_aspect_ratio": "1:1", "r_frame_rate": "50/1", "nb_frames": "500",
             "tags": {{"timecode": "10:00:00:00"}}}}
    override = path.with_suffix(".facts.json")
    if override.exists():
        extra = json.loads(override.read_text())
        video["tags"].update(extra.pop("tags", {{}}))
        video.update(extra)
    print(json.dumps({{"streams": [video], "format": {{"filename": str(path), "tags": {{}}}}}}))
    sys.exit(0)
print(json.dumps({{"streams": [{{"codec_type": "audio", "sample_rate": "48000", "channels": 2, "bits_per_sample": 24,
                               "duration_ts": 4800}}],
                  "format": {{"tags": {{"time_reference": "1769280123"}}}}}}))
'''

FFMPEG = '''#!{python}
print("ffmpeg version 7.1.1 Copyright (c) 2000-2025 the FFmpeg developers")
'''


def script(path: Path, body: str) -> None:
    path.write_text(body.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def seal(folder: Path) -> None:
    done = subprocess.run([str(ASCMHL), "create", "-h", "xxh64", str(folder)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def r3d(roll: Path, clip: str, parts: int = 1, facts: dict | None = None) -> None:
    rdc = roll / f"{clip}.RDC"
    rdc.mkdir(parents=True, exist_ok=True)
    for part in range(1, parts + 1):
        (rdc / f"{clip}_{part:03d}.R3D").write_bytes(b"r" * 100 * part)
    if facts:
        (rdc / f"{clip}.facts.json").write_text(json.dumps(facts))


def install_stub_tools(bin_dir: Path) -> None:
    for name, body in (("r3dtool", R3DTOOL), ("ffprobe", FFPROBE), ("ffmpeg", FFMPEG)):
        script(bin_dir / name, body)


def mov(roll: Path, clip: str, parts: int = 1, facts: dict | None = None) -> None:
    """ProRes movie parts. facts is one dict for every part, or a list with one dict per part."""
    rdc = roll / f"{clip}.RDC"
    rdc.mkdir(parents=True, exist_ok=True)
    for part in range(1, parts + 1):
        path = rdc / f"{clip}_{part:03d}.mov"
        path.write_bytes(b"m" * 100 * part)
        chosen = facts[part - 1] if isinstance(facts, list) else facts
        if chosen:
            path.with_suffix(".facts.json").write_text(json.dumps(chosen))
